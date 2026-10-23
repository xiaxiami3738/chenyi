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
    // 点击徽章 → 弹出当前模式的完整说明
    badge.addEventListener("click", () => showModePopover(data));
    badge.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        showModePopover(data);
      }
    });
  } catch (err) {
    badge.textContent = "服务不可达";
    badge.className = "badge badge-muted";
  }
}

function showModePopover(data) {
  const pop = $("mode-popover");
  if (!pop) return;
  const title = $("mode-popover-title");
  const body = $("mode-popover-body");
  if (data.demo) {
    title.textContent = "Demo 模式（当前站点）";
    body.textContent =
      "当前站点运行在 Demo 模式：无论你是否填入 Key，生成的内容都是内置的预置示例数据（FakeLLM），" +
      "不调用任何真实模型，零费用、零额度消耗。是否真实模式由站长在服务器端决定，与你在浏览器里保存什么无关。" +
      "你现在填的 Key 只会保存在你自己的浏览器 localStorage，本站不会上传、不会落库、Demo 模式下也不会被调用。";
  } else {
    title.textContent = "真实模式（当前站点）";
    body.textContent =
      "站点已开启真实模式（BYOK）：每次生成你必须自带模型 Key（X-LLM-Key 请求头单次透传），" +
      "本站不落库、不写日志，只在服务端转发给你选择的供应商（如 DeepSeek/Qwen/GLM 等），随后即被丢弃。" +
      "可用模型由你选择的供应商决定。";
  }
  pop.hidden = false;
  $("mode-badge").setAttribute("aria-expanded", "true");
}

function closeModePopover() {
  const pop = $("mode-popover");
  if (pop) pop.hidden = true;
  $("mode-badge").setAttribute("aria-expanded", "false");
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
let currentSessionId = null; // 最近一次成功生成的 session_id（供下载 Markdown）

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
  const actions = $("gen-actions");
  if (actions) actions.hidden = true;
  $("gen-progress").innerHTML = "";
  $("gen-days").innerHTML = "";
  $("gen-links").innerHTML = "";
  currentSessionId = null;
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

  // 生成完成：亮出「下载 Markdown / 查看历史」操作按钮
  const actions = $("gen-actions");
  if (actions) actions.hidden = false;
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
    currentSessionId = data.session_id;
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

/* ── M3：下载 Markdown 与历史页 ───────────────────────── */

async function downloadMarkdown() {
  /* 把最近一次生成的会话导出为 Markdown 附件（export 是 GET，不进创建限流）。 */
  if (!currentSessionId) {
    showError("还没有可导出的会话，请先生成一次。");
    return;
  }
  try {
    const res = await fetch(`/api/sessions/${currentSessionId}/export.md`);
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const msg = (data && data.error && data.error.message) || "导出失败，请稍后再试。";
      throw new Error(msg);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `plan-${currentSessionId}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    showError(err.message || "下载失败。");
  }
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
}

const STATUS_LABEL = { generating: "生成中", ready: "已完成", failed: "失败" };

function sessionBadge(status) {
  const badge = document.createElement("span");
  badge.className = "badge " + (status === "ready" ? "badge-ok" : "badge-muted");
  badge.textContent = STATUS_LABEL[status] || status;
  return badge;
}

function buildSessionMeta(s) {
  const st = s.stats || {};
  return `${fmtTime(s.created_at)} · 计划 ${st.plan_count || 0} 天 · 讲义 ${st.lesson_count || 0} 篇 · 名词 ${st.term_count || 0} 张 · 练习 ${st.quiz_count || 0} 道`;
}

function sessionMarkdownBtn(sessionId, label) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-ghost btn-small";
  btn.textContent = label || "📥 下载 Markdown";
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    currentSessionId = sessionId;
    downloadMarkdown();
  });
  return btn;
}

async function loadHistory() {
  /* 历史页：按当前 client_id 拉取会话列表并渲染卡片。 */
  const list = $("history-list");
  if (!list) return;
  list.innerHTML = "<p class='muted'>加载中…</p>";
  try {
    const clientId = ensureClientId();
    const res = await fetch(`/api/sessions?client_id=${encodeURIComponent(clientId)}&limit=50`);
    const data = await res.json();
    if (!res.ok) {
      const msg = (data.error && data.error.message) || "加载历史失败，请稍后再试。";
      throw new Error(msg);
    }
    const sessions = data.sessions || [];
    $("history-empty").hidden = sessions.length > 0;
    list.innerHTML = "";
    sessions.forEach((s) => {
      const card = document.createElement("article");
      card.className = "session-card";
      const top = document.createElement("div");
      top.className = "session-card-top";
      top.append(textEl("h3", "session-goal", s.goal || "（无目标）"), sessionBadge(s.status));
      card.append(top, textEl("p", "session-meta", buildSessionMeta(s)));
      const actions = textEl("div", "session-actions", "");
      actions.appendChild(sessionMarkdownBtn(s.id));
      card.appendChild(actions);
      card.addEventListener("click", () => showHistoryDetail(s.id));
      list.appendChild(card);
    });
  } catch (err) {
    list.innerHTML = `<p class='muted'>${err.message}</p>`;
  }
}

function renderHistoryDetail(summary) {
  /* 复用 /api/sessions/{id} 摘要：目标 + 状态 + 统计 + 下载按钮 + 策展链接。 */
  const root = $("detail-root");
  root.innerHTML = "";
  const s = summary.session || {};
  const st = summary.stats || {};
  root.appendChild(textEl("h3", "session-goal", s.goal || "（无目标）"));
  root.appendChild(
    textEl("p", "session-meta", `状态：${STATUS_LABEL[s.status] || s.status} · 生成于 ${fmtTime(s.created_at)}`)
  );
  root.appendChild(textEl("p", "session-meta", buildSessionMeta({ stats: st })));
  root.appendChild(sessionMarkdownBtn(s.id));
  const links = summary.links || [];
  if (links.length) {
    root.appendChild(textEl("h4", "detail-section", "🗂 配套开源项目（人工策展）"));
    links.forEach((link) => {
      const item = textEl("div", "link-item", "");
      const a = document.createElement("a");
      a.href = link.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = link.title;
      item.append(a, textEl("div", "why", link.why || ""));
      root.appendChild(item);
    });
  }
}

async function showHistoryDetail(sessionId) {
  /* 点击历史卡片：拉取该会话摘要并展示在当前页内。 */
  const detail = $("history-detail");
  detail.hidden = false;
  $("detail-root").innerHTML = "<p class='muted'>加载中…</p>";
  try {
    const res = await fetch(`/api/sessions/${sessionId}`);
    const data = await res.json();
    if (!res.ok) {
      const msg = (data.error && data.error.message) || "加载详情失败。";
      throw new Error(msg);
    }
    renderHistoryDetail(data);
    detail.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    $("detail-root").innerHTML = `<p class='muted'>${err.message}</p>`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  ensureClientId();
  loadHealth();
  if ($("history-list")) {
    // 历史页：只做列表加载与刷新，不触碰首页专属元素
    loadHistory();
    $("history-refresh").addEventListener("click", loadHistory);
    return;
  }
  loadProviders();
  restoreKeyFields();
  loadLinks("");
  bindForm();
  checkInsecure();
  $("provider").addEventListener("change", syncProviderDefaults);
  $("save-key").addEventListener("click", saveKeyFields);
  $("clear-key").addEventListener("click", clearKeyFields);
  const downloadBtn = $("download-md");
  if (downloadBtn) downloadBtn.addEventListener("click", downloadMarkdown);
  const popClose = $("mode-popover-close");
  if (popClose) popClose.addEventListener("click", closeModePopover);
  document.addEventListener("click", (ev) => {
    if (!$("mode-popover") || $("mode-popover").hidden) return;
    if (!$("mode-popover").contains(ev.target) && !$("mode-badge").contains(ev.target)) {
      closeModePopover();
    }
  });
});