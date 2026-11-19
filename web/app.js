const main = document.querySelector("#main");
const identity = document.querySelector("#identity");
const switchButton = document.querySelector("#switch");
const toast = document.querySelector("#toast");
let current = null;
let polling = null;

function element(tag, text, cls) {
  const node = document.createElement(tag);
  node.textContent = text ?? "";
  if (cls) node.className = cls;
  return node;
}

async function api(path, data) {
  const options = data === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)
  };
  const response = await fetch("/api/" + path, options);
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.error), {status: response.status});
  return result;
}

function inform(message) {
  toast.replaceChildren(element("span", message));
  toast.hidden = false;
  const close = element("button", "닫기");
  close.onclick = () => { toast.hidden = true; };
  toast.append(close);
}

function form(id, submit) {
  document.querySelector("#" + id).onsubmit = async event => {
    event.preventDefault();
    const target = event.currentTarget;
    const button = target.querySelector("button");
    button.disabled = true;
    try { await submit(Object.fromEntries(new FormData(target)), target); }
    catch (error) { inform(error.message); }
    finally { button.disabled = false; }
  };
}

function template(id) {
  main.replaceChildren(document.querySelector("template#" + id).content.cloneNode(true));
}

const date = stamp => stamp ? new Date(stamp * 1000).toLocaleString("ko-KR") : "접속 전";

function table(target, headings, rows) {
  const holder = element("div", "", "tablewrap");
  const grid = element("table");
  grid.setAttribute("role", "table");
  const head = grid.createTHead().insertRow();
  for (const title of headings) {
    const heading = element("th", title, title === "관리" ? "no-print" : "");
    heading.scope = "col";
    head.append(heading);
  }
  const body = grid.createTBody();
  for (const row of rows) {
    const line = body.insertRow();
    for (const value of row) {
      const cell = line.insertCell();
      const label = element("span", headings[cell.cellIndex], "mobile-field");
      label.setAttribute("aria-hidden", "true");
      cell.append(label);
      if (value instanceof Node) {
        cell.append(value);
        if (headings[cell.cellIndex] === "관리") cell.classList.add("no-print");
      }
      else cell.append(element("span", value));
    }
  }
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = headings.length;
    cell.textContent = "아직 기록이 없습니다.";
  }
  holder.append(grid);
  target.replaceChildren(holder);
}

async function history(questions) {
  const records = await api("history");
  const titles = new Map(questions.map(q => [q.id, q.title]));
  table(document.querySelector("#history"), ["시각", "문항", "결과"],
    records.map(a => [date(a.stamp), titles.get(a.question) || a.question, a.correct ? "정답" : "오답"]));
}

function shuffled(items) {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

function quiz(questions, index = 0, correct = 0, missed = []) {
  const panel = document.querySelector("#quiz");
  if (index === questions.length) {
    const retry = element("button", "다시 연습하기");
    retry.onclick = () => quiz(questions);
    panel.replaceChildren(element("h2", "학습을 마쳤습니다"), element("p", `${questions.length}문항 중 ${correct}문항 정답`), retry);
    if (missed.length) {
      const review = element("button", `오답 ${missed.length}문항 다시 풀기`);
      review.onclick = () => quiz(missed);
      panel.append(" ", review);
    }
    panel.querySelector("h2").tabIndex = -1;
    panel.querySelector("h2").focus();
    return;
  }
  const q = questions[index];
  let selected = -1;
  const progress = element("progress");
  progress.max = questions.length;
  progress.value = index;
  progress.setAttribute("aria-label", "학습 진행");
  const choices = element("div", "", "choices");
  const submit = element("button", "정답 확인");
  submit.disabled = true;
  const order = shuffled(q.choices.map((_, i) => i));
  const buttons = [];
  for (const choice of order) {
    const label = q.choices[choice];
    const button = element("button", label, "choice");
    button.setAttribute("aria-pressed", "false");
    button.onclick = () => {
      selected = choice;
      buttons.forEach((node, i) => node.setAttribute("aria-pressed", String(i === choice)));
      submit.disabled = false;
    };
    choices.append(button);
    buttons[choice] = button;
  }
  const heading = element("h2", q.title);
  heading.tabIndex = -1;
  panel.replaceChildren(element("small", `${index + 1} / ${questions.length}`), progress,
    heading, choices, submit);
  heading.focus();
  submit.onclick = async () => {
    submit.disabled = true;
    buttons.forEach(button => { button.disabled = true; });
    try {
      const result = await api("answer", {id: q.id, choice: selected});
      buttons[result.answer].classList.add("correct");
      buttons[result.answer].append(element("strong", " ✓ 정답"));
      if (!result.correct) {
        buttons[selected].classList.add("incorrect");
        buttons[selected].append(element("strong", " ✗ 내가 고른 오답"));
      }
      const detail = element("p", (result.correct ? "✓ 정답입니다. " : "✗ 오답입니다. 정답은 ‘" + q.choices[result.answer] + "’입니다. ") + result.feedback, "feedback " + (result.correct ? "correct" : "incorrect"));
      detail.setAttribute("role", "status");
      const next = element("button", index + 1 === questions.length ? "결과 보기" : "다음 문항");
      next.onclick = () => quiz(questions, index + 1, correct + Number(result.correct), result.correct ? missed : [...missed, q]);
      submit.replaceWith(detail, next);
      next.focus();
      history(current.questions).catch(error => inform(error.message));
    } catch (error) {
      inform(error.message);
      buttons.forEach(button => { button.disabled = false; });
      submit.disabled = false;
    }
  };
}

async function refreshReport() {
  const report = await api("admin/report");
  const users = document.querySelector("#users");
  const expanded = new Set([...users.querySelectorAll("details[open]")].map(node => node.dataset.id));
  const groups = new Map(report.users.map(user => [user.id, []]));
  for (const attempt of report.attempts) groups.get(attempt.uid)?.push(attempt);
  users.replaceChildren(...report.users.map(user => {
    const rows = groups.get(user.id);
    const group = element("details", "", "learner-report");
    group.dataset.id = user.id;
    group.open = expanded.has(String(user.id));
    const summary = element("summary", `${user.name} · 최근 로그인 ${date(user.last_seen)} · ${rows.length}회 응답`);
    const content = element("div");
    table(content, ["응답 시각", "문제", "선택한 답", "정답", "결과", "관리"],
      rows.map(a => [date(a.stamp), a.prompt || a.question, a.selected || "기존 기록에 없음",
        a.expected || "기존 기록에 없음", a.correct ? "정답" : "오답", deleteButton(a.id)]));
    group.append(summary, content);
    return group;
  }));
  if (!report.users.length) users.append(element("p", "등록된 학습자가 없습니다."));
  const select = document.querySelector("#notice select");
  const previous = select.value;
  select.replaceChildren(new Option("전체 학습자", "0"), ...report.users.map(u => new Option(u.name, u.id)));
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
  document.querySelector("#report-date").textContent = "조회 시각 " + date(Date.now() / 1000);
}

function deleteButton(id) {
  const button = element("button", "삭제", "no-print");
  button.onclick = async () => {
    if (!window.confirm("이 응답 한 건을 학습 이력과 보고서에서 삭제할까요?")) return;
    button.disabled = true;
    try {
      await api("admin/attempt-delete", {id});
      await refreshReport();
      inform("응답을 학습 이력에서 삭제했습니다.");
    } catch (error) { inform(error.message); button.disabled = false; }
  };
  return button;
}

async function loadEvidence() {
  const data = await api("admin/evidence");
  const holder = document.querySelector("#evidence-content");
  const list = element("ul");
  for (const ref of data.references) {
    const item = element("li");
    if (/^https?:\/\//i.test(ref.url || "")) {
      const link = element("a", ref.title || ref.url);
      link.href = ref.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
    } else item.append(ref.title || ref.area);
    item.append(element("p", [ref.area, ref.status, ref.summary].filter(Boolean).join(" · ")));
    if (ref.quote) item.append(element("blockquote", ref.quote));
    list.append(item);
  }
  holder.replaceChildren(list);
  const questions = element("div");
  table(questions, ["단계", "문제", "매뉴얼 위치"],
    data.questions.map(q => [q.category, q.title, q.source]));
  holder.append(questions);
}

function showNotice(state) {
  const notice = state.notices[0];
  if (!notice || !toast.hidden) return;
  inform(notice.message);
  toast.querySelector("button").onclick = async () => {
    try { await api("seen", {id: notice.id}); toast.hidden = true; }
    catch (error) { inform(error.message); }
  };
}

async function open() {
  clearInterval(polling);
  toast.hidden = true;
  try { current = await api("state"); }
  catch (error) {
    current = null;
    switchButton.hidden = true;
    identity.textContent = "내부 시연";
    template("login");
    if (error.status !== 401) inform(error.message);
    form("signin", async data => { await api("login", data); await open(); });
    return;
  }
  switchButton.hidden = false;
  switchButton.textContent = current.demo_switch
    ? (current.role === "admin" ? "학습자 모드로 전환" : "관리자 모드로 전환")
    : "다른 계정으로 전환";
  identity.textContent = current.name + (current.role === "admin" ? " · 관리자" : " · 학습자");
  if (current.role === "admin") {
    template("admin");
    document.querySelector("#evidence").addEventListener("toggle", event => {
      if (event.currentTarget.open) loadEvidence().catch(error => inform(error.message));
    });
    document.querySelector("#refresh").onclick = () => refreshReport().catch(error => inform(error.message));
    document.querySelector("#print").onclick = async () => {
      try {
        await refreshReport();
        const collapsed = [...document.querySelectorAll("#users details:not([open])")];
        collapsed.forEach(node => { node.open = true; });
        window.addEventListener("afterprint", () => collapsed.forEach(node => { node.open = false; }), {once: true});
        window.print();
      } catch (error) { inform(error.message); }
    };
    form("create", async (data, target) => { await api("admin/users", data); target.reset(); await refreshReport(); inform("학습자 계정을 만들었습니다."); });
    form("notice", async (data, target) => {
      const result = await api("admin/notice", {...data, target: Number(data.target)});
      target.elements.message.value = "";
      inform(`${result.count}명에게 알림을 보냈습니다.`);
    });
    await refreshReport();
  } else {
    template("learner");
    const questions = await api("questions");
    current.questions = questions;
    const category = document.querySelector("#category");
    const counts = new Map();
    for (const q of questions) counts.set(q.category, (counts.get(q.category) || 0) + 1);
    category.replaceChildren(new Option(`전체 범위 · ${questions.length}문항`, ""),
      ...[...counts].map(([name, count]) => new Option(`${name} · ${count}문항`, name)));
    const pool = () => questions.filter(q => !category.value || q.category === category.value);
    document.querySelector("#quiz").append(element("p", "학습 범위와 풀이 방식을 선택하세요."));
    document.querySelector("#quick").onclick = () => quiz(shuffled(pool()).slice(0, 1));
    document.querySelector("#regular").onclick = () => quiz(shuffled(pool()).slice(0, 8));
    document.querySelector("#all-questions").onclick = () => quiz(shuffled(pool()));
    const settings = document.querySelector("#settings");
    settings.elements.minutes.value = current.reminder;
    form("settings", async data => { await api("reminder", {minutes: Number(data.minutes)}); inform("알림 설정을 저장했습니다."); });
    showNotice(current);
    polling = setInterval(async () => {
      try { showNotice(await api("state")); }
      catch (error) { if (error.status === 401) await open(); }
    }, 3000);
    await history(questions);
  }
}

switchButton.onclick = async () => {
  try { await api(current?.demo_switch ? "switch-demo" : "logout", {}); await open(); }
  catch (error) { if (error.status === 401) await open(); else inform(error.message); }
};
open().catch(error => inform(error.message));
