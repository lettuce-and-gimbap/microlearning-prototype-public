import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from getpass import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("LEARNING_DB", ROOT / "data" / "prototype.sqlite"))
SECURE = os.environ.get("LEARNING_HTTPS") == "1"
BANK_ORDER = ("questions-prep.json", "questions-observe.json", "questions-response.json",
              "questions-followup.json", "questions.json")
QUESTIONS = [
    question
    for name in BANK_ORDER
    if (path := ROOT / name).is_file()
    for question in json.loads(path.read_text(encoding="utf-8-sig"))
]
QUESTION_BY_ID = {question["id"]: question for question in QUESTIONS}
if len(QUESTION_BY_ID) != len(QUESTIONS) or any(
    len(question["choices"]) != 4 or len(set(question["choices"])) != 4
    or type(question["answer"]) is not int or not 0 <= question["answer"] < 4
    for question in QUESTIONS
):
    raise ValueError("Question bank contains duplicate IDs or invalid choices")
DEMO_PAIR = {"테스트1": "테스트2", "테스트2": "테스트1"}


@contextmanager
def database():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                          n=16384, r=8, p=1).hex()


def add_user(db, name, password, role):
    salt = secrets.token_hex(16)
    db.execute("INSERT INTO users(name, salt, hash, role) VALUES(?,?,?,?)",
               (name, salt, password_hash(password, salt), role))


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, salt TEXT NOT NULL,
                hash TEXT NOT NULL, role TEXT NOT NULL, last_seen INTEGER,
                reminder INTEGER NOT NULL DEFAULT 30, next_notice INTEGER);
            CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY, uid INTEGER REFERENCES users(id),
                expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(
                id INTEGER PRIMARY KEY, uid INTEGER REFERENCES users(id),
                question TEXT, correct INTEGER, stamp INTEGER);
            CREATE TABLE IF NOT EXISTS notices(
                id INTEGER PRIMARY KEY, uid INTEGER REFERENCES users(id),
                message TEXT, stamp INTEGER, seen INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS login_limits(
                key TEXT PRIMARY KEY, window INTEGER, count INTEGER);
            CREATE TABLE IF NOT EXISTS attempt_deletions(
                attempt_id INTEGER PRIMARY KEY, actor TEXT NOT NULL, stamp INTEGER NOT NULL);
        """)
        columns = {column["name"] for column in db.execute("PRAGMA table_info(attempts)")}
        for column in ("prompt", "selected", "expected"):
            if column not in columns:
                db.execute(f"ALTER TABLE attempts ADD COLUMN {column} TEXT")
        if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            password = os.environ.get("LEARNING_ADMIN_PASSWORD") or secrets.token_urlsafe(18)
            if len(password) < 12 or len(password) > 128:
                raise ValueError("Administrator password must be 12–128 characters")
            add_user(db, "admin", password, "admin")
            if "LEARNING_ADMIN_PASSWORD" not in os.environ:
                print("Initial admin password:", password, flush=True)


class Request(BaseHTTPRequestHandler):
    def sign_in(self, db, uid, token, now, cookie):
        raw = secrets.token_hex(32)
        db.execute("DELETE FROM sessions WHERE expires<=? OR token=?", (now, token))
        db.execute("INSERT INTO sessions VALUES(?,?,?)",
                   (hashlib.sha256(raw.encode()).hexdigest(), uid, now+28800))
        db.execute("UPDATE users SET last_seen=? WHERE id=?", (now, uid))
        db.commit()
        return self.reply({"ok": True}, cookie="sid="+raw+cookie+"; Max-Age=28800")

    def demo_switch_allowed(self):
        return (not SECURE and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                and not any(self.headers.get(h) for h in ("Forwarded", "X-Forwarded-For", "CF-Connecting-IP")))

    def reply(self, value, status=200, cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie + ("; Secure" if SECURE else ""))
        self.end_headers()
        self.wfile.write(json.dumps(value, ensure_ascii=False).encode())

    def do_GET(self):
        files = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
                 "/app.js": ("app.js", "text/javascript")}
        if self.path in files:
            name, mime = files[self.path]
            file = ROOT / "web" / name
            if not file.is_file():
                return self.reply({"error": "파일 없음"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(file.read_bytes())
            return
        self.api({})

    def do_POST(self):
        if self.headers.get_content_type() != "application/json":
            return self.reply({"error": "JSON 요청이 필요합니다."}, 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                return self.reply({"error": "요청 크기가 잘못되었습니다."}, 400)
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return self.reply({"error": "잘못된 요청입니다."}, 400)
        self.api(data)

    def api(self, data):
        now = int(time.time())
        post = self.command == "POST"
        path = self.path
        match = re.search(r"(?:^|;\s*)sid=([0-9a-f]{64})(?:;|$)", self.headers.get("Cookie", ""))
        token = hashlib.sha256(match[1].encode()).hexdigest() if match else ""
        cookie = "; Path=/; HttpOnly; SameSite=Strict"
        with database() as db:
            if post and path == "/api/login":
                key = self.client_address[0]
                db.execute("DELETE FROM login_limits WHERE window<=?", (now-900,))
                db.execute("""INSERT INTO login_limits VALUES(?,?,1)
                    ON CONFLICT(key) DO UPDATE SET count=count+1""", (key, now))
                if db.execute("SELECT count FROM login_limits WHERE key=?", (key,)).fetchone()[0] > 20:
                    return self.reply({"error": "잠시 뒤 다시 시도하세요."}, 429)
                user = db.execute("SELECT * FROM users WHERE name=?", (str(data.get("name", "")),)).fetchone()
                password = str(data.get("password", ""))
                if len(password) > 128:
                    return self.reply({"error": "로그인 정보를 확인하세요."}, 401)
                candidate = password_hash(password, user["salt"] if user else "0"*32)
                if not user or not hmac.compare_digest(candidate, user["hash"]):
                    return self.reply({"error": "로그인 정보를 확인하세요."}, 401)
                if user["name"] in DEMO_PAIR and not self.demo_switch_allowed():
                    return self.reply({"error": "테스트 계정은 로컬에서만 사용할 수 있습니다."}, 403)
                db.execute("DELETE FROM login_limits WHERE key=?", (key,))
                return self.sign_in(db, user["id"], token, now, cookie)

            user = db.execute("""SELECT users.* FROM users JOIN sessions
                ON sessions.uid=users.id WHERE token=? AND expires>?""", (token, now)).fetchone()
            if not user:
                return self.reply({"error": "로그인이 필요합니다."}, 401)
            uid = user["id"]
            admin = user["role"] == "admin"
            demo = user["name"] in DEMO_PAIR and self.demo_switch_allowed()
            if user["name"] in DEMO_PAIR and not demo:
                return self.reply({"error": "테스트 계정은 로컬에서만 사용할 수 있습니다."}, 403)
            if post and path == "/api/switch-demo":
                if not demo:
                    return self.reply({"error": "테스트 계정에서만 전환할 수 있습니다."}, 403)
                target = db.execute("SELECT id FROM users WHERE name=? AND role=?",
                    (DEMO_PAIR[user["name"]], "learner" if admin else "admin")).fetchone()
                if not target:
                    return self.reply({"error": "전환할 계정이 없습니다."}, 404)
                return self.sign_in(db, target["id"], token, now, cookie)
            if post and path == "/api/logout":
                db.execute("DELETE FROM sessions WHERE token=?", (token,))
                return self.reply({"ok": True}, cookie="sid="+cookie+"; Max-Age=0")
            if not post and path == "/api/state":
                if not admin and user["reminder"]:
                    db.execute("""UPDATE users SET next_notice=? WHERE id=?
                        AND next_notice IS NULL""", (now+user["reminder"]*60, uid))
                    due = db.execute("""UPDATE users SET next_notice=? WHERE id=? AND next_notice<=?""",
                                     (now+user["reminder"]*60, uid, now)).rowcount
                    if due:
                        db.execute("INSERT INTO notices(uid,message,stamp) VALUES(?,?,?)",
                                   (uid, "복습할 시간입니다. 오늘의 학습을 열어보세요.", now))
                notices = db.execute("SELECT id,message FROM notices WHERE uid=? AND seen=0 ORDER BY id",
                                     (uid,)).fetchall()
                return self.reply({"name": user["name"], "role": user["role"],
                    "reminder": user["reminder"], "demo_switch": demo, "notices": [dict(n) for n in notices]})
            if post and path == "/api/seen":
                notice = data.get("id")
                if type(notice) is not int or not 0 < notice < 2**63:
                    return self.reply({"error": "알림 번호가 잘못되었습니다."}, 400)
                db.execute("UPDATE notices SET seen=1 WHERE id=? AND uid=?", (notice, uid))
                return self.reply({"ok": True})
            if path.startswith("/api/admin/") and not admin:
                return self.reply({"error": "관리자 권한이 필요합니다."}, 403)
            if admin:
                return self.admin(db, path, post, data, now, user["name"])
            if not post and path == "/api/questions":
                return self.reply([{"id": q["id"], "title": q["title"], "choices": q["choices"],
                                    "category": q.get("category", "역할과 교육")} for q in QUESTIONS])
            if not post and path == "/api/history":
                return self.reply([dict(a) for a in db.execute(
                    """SELECT question,correct,stamp FROM attempts WHERE uid=?
                    AND NOT EXISTS (SELECT 1 FROM attempt_deletions WHERE attempt_id=attempts.id)
                    ORDER BY id DESC LIMIT 100""", (uid,))])
            if post and path == "/api/answer":
                question_id = data.get("id")
                question = QUESTION_BY_ID.get(question_id) if isinstance(question_id, str) else None
                choice = data.get("choice")
                if not question or type(choice) is not int or not 0 <= choice < 4:
                    return self.reply({"error": "문항 또는 선택지를 확인하세요."}, 400)
                correct = choice == question["answer"]
                db.execute("""INSERT INTO attempts(uid,question,correct,stamp,prompt,selected,expected)
                    VALUES(?,?,?,?,?,?,?)""", (uid, question["id"], correct, now, question["title"],
                    question["choices"][choice], question["choices"][question["answer"]]))
                return self.reply({"correct": correct, "answer": question["answer"], "feedback": question["feedback"]})
            if post and path == "/api/reminder":
                interval = data.get("minutes")
                if type(interval) is not int or interval not in (0, 1, 15, 30, 60, 1440):
                    return self.reply({"error": "알림 간격을 확인하세요."}, 400)
                db.execute("UPDATE users SET reminder=?, next_notice=? WHERE id=?",
                           (interval, now+interval*60 if interval else None, uid))
                return self.reply({"ok": True})
            return self.reply({"error": "찾을 수 없습니다."}, 404)

    def admin(self, db, path, post, data, now, actor):
        if not post and path == "/api/admin/evidence":
            evidence = ROOT / "evidence.json"
            return self.reply({
                "references": json.loads(evidence.read_text(encoding="utf-8-sig")) if evidence.exists() else [],
                "questions": [{"id": q["id"], "title": q["title"], "source": q.get("source", ""),
                               "category": q.get("category", "역할과 교육")} for q in QUESTIONS],
            })
        if post and path == "/api/admin/attempt-delete":
            attempt = data.get("id")
            if type(attempt) is not int or not 0 < attempt < 2**63:
                return self.reply({"error": "기록 번호를 확인하세요."}, 400)
            deleted = db.execute("""INSERT OR IGNORE INTO attempt_deletions
                SELECT attempts.id,?,? FROM attempts JOIN users ON users.id=attempts.uid
                WHERE attempts.id=? AND users.role='learner'""",
                (actor, now, attempt)).rowcount
            db.commit()
            return self.reply({"ok": bool(deleted)})
        if not post and path == "/api/admin/report":
            users = db.execute("SELECT id,name,last_seen FROM users WHERE role='learner' ORDER BY name").fetchall()
            attempts = db.execute("""SELECT attempts.*, users.name FROM attempts
                JOIN users ON users.id=attempts.uid WHERE users.role='learner'
                AND NOT EXISTS (SELECT 1 FROM attempt_deletions WHERE attempt_id=attempts.id)
                ORDER BY attempts.id DESC""").fetchall()
            records = []
            for row in attempts:
                record = dict(row)
                question = QUESTION_BY_ID.get(record["question"])
                if not record["prompt"] and question:
                    record["prompt"] = question["title"]
                records.append(record)
            return self.reply({"users": [dict(u) for u in users], "attempts": records})
        if post and path == "/api/admin/users":
            name, password = str(data.get("name", "")), str(data.get("password", ""))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{3,32}", name) or not 12 <= len(password) <= 128:
                return self.reply({"error": "계정 ID는 영문·숫자 등 3~32자, 비밀번호는 12~128자입니다."}, 400)
            try:
                add_user(db, name, password, "learner")
            except sqlite3.IntegrityError:
                return self.reply({"error": "이미 등록된 계정입니다."}, 409)
            return self.reply({"ok": True})
        if post and path == "/api/admin/notice":
            message = str(data.get("message", "")).strip()
            target = data.get("target")
            if not 1 <= len(message) <= 300 or type(target) is not int or not 0 <= target < 2**63:
                return self.reply({"error": "대상과 알림 내용을 확인하세요."}, 400)
            count = db.execute("""INSERT INTO notices(uid,message,stamp)
                SELECT id,?,? FROM users WHERE role='learner' AND (?=0 OR id=?)""",
                               (message, now, target, target)).rowcount
            return self.reply({"count": count})
        return self.reply({"error": "찾을 수 없습니다."}, 404)


if __name__ == "__main__":
    initialize()
    if sys.argv[1:] == ["reset-admin"]:
        password = getpass("New admin password (12–128 characters): ")
        if not 12 <= len(password) <= 128:
            raise SystemExit("Invalid password length")
        salt = secrets.token_hex(16)
        with database() as db:
            db.execute("UPDATE users SET salt=?, hash=? WHERE name='admin'",
                       (salt, password_hash(password, salt)))
            db.execute("DELETE FROM sessions WHERE uid=(SELECT id FROM users WHERE name='admin')")
        print("Administrator password updated.")
        raise SystemExit(0)
    if os.environ.get("LEARNING_HOST", "127.0.0.1") != "127.0.0.1":
        raise SystemExit("Run on loopback and use an authenticated HTTPS reverse proxy for remote testing.")
    port = int(os.environ.get("PORT", "8765"))
    print(f"http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Request).serve_forever()
