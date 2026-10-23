/* 前端脚本：展示状态、在本机保存 BYOK 配置、渲染策展链接。
   M1：提交表单后 POST /api/sessions，再用 EventSource 订阅 SSE，
   逐条渲染 progress / lesson / term / quiz / done / error 事件。 */

const LS = {
  provider: "chenyi.provider",
  model: "chenyi.model",
  baseUrl: "chenyi.baseUrl",
  key: "chenyi.key",
  clientId: "chenyi.clientId",
};

const $ = (id) => document.getElementById(id);

function ensureClientId() {
  let id = localStorage.getItem(LS.clientId);
  if (!id) {
    id = "c_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    localStorage.setItem(LS.clientId, id);
  }
  return id;
}

async function loadHealth() {
  const badge = $("mode-badge");
  try {
    const res = await fetch("/api/healthz");
    const data = await res.json();
    badge.textContent = data.demo ? "Demo 模式（无需 Key）" : "真实模式";
    badge.className = "badge " + (data.demo ? "badge-demo" : "badge-ok");
  } catch (err) {
    badge.textContent = "服务不可达";
    badge.className = "badge badge-muted";
  }
}

async function loadProviders() {
  const select = $("provider");
  try {
    const res = await fetch("/api/providers");
    const data = await res.json();
    select.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "（不填，用 Demo 预置内容）";
    select.appendChild(none);
    data.providers.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      opt.dataset.baseUrl = p.base_url;
      opt.dataset.model = p.default_model;
      select.appendChild(opt);
    });
    select.value = localStorage.getItem(LS.provider) || "";
    syncProviderDefaults();
  } catch (err) {
    select.innerHTML = "<option value=''>无法加载供应商列表</option>";
  }
}

function syncProviderDefaults() {
  const select = $("provider");
  const opt = select.selectedOptions[0];
  const modelInput = $("model");
  if (opt && opt.dataset.model) {
    modelInput.placeholder = "默认：" + opt.dataset.model;
  }
}

function restoreKeyFields() {
  $("model").value = localStorage.getItem(LS.model) || "";
  $("base-url").value = localStorage.getItem(LS.baseUrl) || "";
  const key = localStorage.getItem(LS.key);
  if (key) {
    $("api-key").value = key;
    $("key-status").textContent = "已从本机浏览器读取已保存的 Key（不会上传给本站）。";
  }
}

function saveKeyFields() {
  localStorage.setItem(LS.provider, $("provider").value);
  localStorage.setItem(LS.model, $("model").value);
  localStorage.setItem(LS.baseUrl, $("base-url").value);
  const key = $("api-key").value.trim();
  if (key) {
    localStorage.setItem(LS.key, key);
    $("key-status").textContent = "已保存在本机浏览器 localStorage。";
  } else {
    localStorage.removeItem(LS.key);
    $("key-status").textContent = "未填写 Key，将以 Demo 模式运行。";
  }
}

function clearKeyFields() {
  Object.values(LS).forEach((k) => {
    if (k !== LS.clientId) localStorage.removeItem(k);
  });
  $("api-key").value = "";
  $("model").value = "";
  $("base-url").value = "";
  $("key-status").textContent = "已清除本机保存的 Key 与模型配置。";
}

/* ── 生成主流程：提交 → 建会话 → SSE 逐条渲染 ───────────────── */

let genSource = null; // 当前 EventSource，便于中断

function currentPayload() {
  return {
    client_id: ensureClientId(),
    goal: $("goal").value.trim(),
    level: $("level").value,
    weak_points: $("weak_points").value.trim(),
    daily_minutes: Number($("daily_minutes").value || 30),
    provider_id: $("provider").value || null,
    base_url: $("base-url").value.trim() || null,
    model: $("model").value.trim() || null,
  };
}

function setGenerating(on) {
  $("start-btn").disabled = on;
  $("start-btn").textContent = on ? "正在生成中…" : "生成我的学习路径";
}

function showError(message) {
  const box = $("gen-error");
  box.hidden = false;
  box.textContent = "⚠️ " + message;
}

function clearOutput() {
  $("gen-error").hidden = true;
  $("gen-progress").hidden = true;
  $("gen-links").hidden = true;
  $("gen-progress").innerHTML = "";
  $("gen-days").innerHTML = "";
  $("gen-links").innerHTML = "";
}

function appendProgress(message) {
  $("gen-progress").hidden = false;
  const line = document.createElement("p");
  line.className = "progress-line";
  line.textContent = message;
  $("gen-progress").appendChild(line);
}

/** 根据 day_index 找（或建）对应的天卡片容器。 */
function dayCard(day) {
  const daysBox = $("gen-days");
  let card = document.getElementById("day-" + day);
  if (!card) {
    card = document.createElement("article");
    card.id = "day-" + day;
    card.className = "lesson-card";
    card.dataset.day = day;
    daysBox.appendChild(card);
  }
  return card;
}

/** 防止模型/nl 输出里的 HTML 被直接注入，统一用 textContent 渲染。 */
function textEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text;
  return el;
}

function renderLesson(payload) {
  const card = dayCard(payload.day);
  card.innerHTML = ""; // 只保留最新一版
  card.appendChild(textEl("h3", "lesson-title", `第 ${payload.day} 天 · ${payload.topic}`));
  const body = textEl("div", "lesson-body", payload.body_md);
  card.appendChild(body);
  (payload.code_snippets || []).forEach((snip) => {
    card.appendChild(textEl("pre", "code-snippet", snip));
  });
}

function renderTerm(payload) {
  const card = dayCard(payload.day);
  const term = textEl("div", "term-card", "");
  term.appendChild(textEl("strong", "term-name", "📖 " + payload.term));
  term.appendChild(textEl("div", "term-def", payload.definition));
  if (payload.analogy) term.appendChild(textEl("div", "term-analogy", "类比：" + payload.analogy));
  if (payload.pitfalls) term.appendChild(textEl("div", "term-pitfalls", "注意：" + payload.pitfalls));
  card.appendChild(term);
}

function renderQuiz(payload) {
  const card = dayCard(payload.day);
  const quiz = textEl("div", "quiz-card", "");
  quiz.appendChild(textEl("div", "quiz-question", `✏️ ${payload.question}`));
  (payload.options || []).forEach((opt) => {
    quiz.appendChild(textEl("div", "quiz-option", "· " + opt));
  });
  const answer = textEl("div", "quiz-answer", "参考答案：" + payload.answer);
  answer.hidden = true;
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "btn btn-ghost btn-small";
  toggle.textContent = "看答案";
  toggle.addEventListener("click", () => {
    answer.hidden = !answer.hidden;
    toggle.textContent = answer.hidden ? "看答案" : "收起答案";
  });
  quiz.append(toggle, answer);
  card.appendChild(quiz);
}

function renderDone(payload) {
  const done = textEl("div", "done-box", "");
  done.appendChild(textEl("strong", "", "✅ 学习路径已生成完毕"));
  const stats = [
    `计划 ${payload.plan_count || 0} 天`,
    `讲义 ${payload.lesson_count || 0} 篇`,
    `名词 ${payload.term_count || 0} 张`,
    `练习 ${payload.quiz_count || 0} 道`,
  ].join(" · ");
  done.appendChild(textEl("div", "done-stats", stats));
  $("gen-days").appendChild(done);

  const links = payload.links || [];
  if (links.length) {
    const box = $("gen-links");
    box.hidden = false;
    box.appendChild(textEl("h3", "", "🗂 配套开源项目（人工策展）"));
    links.forEach((link) => {
      const item = textEl("div", "link-item", "");
      const a = document.createElement("a");
      a.href = link.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = link.title;
      const why = textEl("div", "why", link.why || "");
      item.append(a, why);
      box.appendChild(item);
    });
  }
}

async function startGeneration(event) {
  event.preventDefault();
  const hint = $("start-hint");
  const payload = currentPayload();
  if (!payload.goal) {
    hint.textContent = "请先写下你的目标。";
    return;
  }
  hint.textContent = "正在创建会话…";
  clearOutput();
  $("start-result").hidden = false;
  setGenerating(true);

  const headers = { "Content-Type": "application/json" };
  const key = $("api-key").value.trim();
  if (key) headers["X-LLM-Key"] = key; // 单次请求透传，服务端不落库

  try {
    const res = await fetch("/api/sessions", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      const msg = (data.error && data.error.message) || "创建会话失败，请稍后再试。";
      throw new Error(msg);
    }
    hint.textContent = "会话已创建（#" + data.session_id + "），正在生成…";
    openEventStream(data.session_id);
  } catch (err) {
    hint.textContent = "";
    showError(err.message || "网络异常，无法创建会话。");
    setGenerating(false);
  }
}

function openEventStream(sessionId) {
  if (genSource) genSource.close();
  const source = new EventSource(`/api/sessions/${sessionId}/events`);
  genSource = source;

  source.addEventListener("progress", (ev) => {
    const payload = JSON.parse(ev.data);
    appendProgress(payload.message || "处理中…");
  });
  source.addEventListener("lesson", (ev) => renderLesson(JSON.parse(ev.data)));
  source.addEventListener("term", (ev) => renderTerm(JSON.parse(ev.data)));
  source.addEventListener("quiz", (ev) => renderQuiz(JSON.parse(ev.data)));
  source.addEventListener("done", (ev) => {
    renderDone(JSON.parse(ev.data));
    source.close();
    genSource = null;
    setGenerating(false);
    $("start-hint").textContent = "生成完成，可以查看上面的结果，或再次提交生成新路径。";
  });
  source.addEventListener("error", (ev) => {
    // SSE error 事件：要么是真实错误事件，要么是连接断开（lastEventId 为空）
    try {
      const payload = JSON.parse(ev.data || "{}");
      showError(payload.message || "生成过程中出错，请重试。");
    } catch (_err) {
      showError("与服务器的实时连接中断，请刷新页面后重试。");
    }
    source.close();
    genSource = null;
    setGenerating(false);
  });
}

function renderTopics(topics, active) {
  const bar = $("topic-bar");
  bar.innerHTML = "";
  const makeChip = (label, value) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "topic-chip" + (value === active ? " active" : "");
    chip.textContent = label;
    chip.addEventListener("click", () => loadLinks(value));
    return chip;
  };
  bar.appendChild(makeChip("全部", ""));
  topics.forEach((t) => bar.appendChild(makeChip(t, t)));
}

async function loadLinks(topic = "") {
  const list = $("links-list");
  list.innerHTML = "<p class='muted'>加载中…</p>";
  try {
    const url = topic ? `/api/links?topic=${encodeURIComponent(topic)}` : "/api/links";
    const res = await fetch(url);
    const data = await res.json();
    renderTopics(data.topics, topic);
    if (!data.links.length) {
      list.innerHTML = "<p class='muted'>该主题暂时没有策展链接。</p>";
      return;
    }
    list.innerHTML = "";
    data.links.forEach((link) => {
      const item = document.createElement("div");
      item.className = "link-item";
      const a = document.createElement("a");
      a.href = link.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = link.title;
      const why = document.createElement("div");
      why.className = "why";
      why.textContent = link.why || "";
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${link.topic} · ${link.url}`;
      item.append(a, why, meta);
      list.appendChild(item);
    });
  } catch (err) {
    list.innerHTML = "<p class='muted'>链接加载失败，请刷新重试。</p>";
  }
}

function bindForm() {
  const form = $("start-form");
  form.addEventListener("submit", startGeneration);
}

function checkInsecure() {
  if (location.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(location.hostname)) {
    $("insecure-warning").hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  ensureClientId();
  loadHealth();
  loadProviders();
  restoreKeyFields();
  loadLinks("");
  bindForm();
  checkInsecure();
  $("provider").addEventListener("change", syncProviderDefaults);
  $("save-key").addEventListener("click", saveKeyFields);
  $("clear-key").addEventListener("click", clearKeyFields);
});