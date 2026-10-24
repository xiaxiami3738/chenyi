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

/* ── 接入配置：只读 /connect 页存入的 localStorage ─────────────────
   首页不再提供供应商/Key 表单（用户要求接入独立到 /connect，避免重复），
   这里只负责读取那份配置，供「生成学习路径」和「鲸小鱼对话」使用。 */

const CONNECT_LS = {
  provider: "chenyi.connect.provider",
  model: "chenyi.connect.model",
  baseUrl: "chenyi.connect.baseUrl",
  apiKey: "chenyi.connect.apiKey",
};

/** 优先读 /connect 页的新键，回退到旧版首页表单留下的老键。 */
function connectValue(kind, legacyKey) {
  const fresh = (localStorage.getItem(CONNECT_LS[kind]) || "").trim();
  if (fresh) return fresh;
  return legacyKey ? (localStorage.getItem(legacyKey) || "").trim() : "";
}

function connectConfig() {
  return {
    providerId: connectValue("provider", LS.provider),
    model: connectValue("model", LS.model),
    baseUrl: connectValue("baseUrl", LS.baseUrl),
    apiKey: connectValue("apiKey", LS.key),
  };
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
    provider_id: connectConfig().providerId || null,
    base_url: connectConfig().baseUrl || null,
    model: connectConfig().model || null,
    use_saved_key: useSavedKeyChecked(),
  };
}

// M7.3：是否勾选「使用我已保存到服务器的 Key」
function useSavedKeyChecked() {
  const el = $("use-saved-key");
  return !!(el && el.checked);
}

function setGenerating(on) {
  $("start-btn").disabled = on;
  $("start-btn").textContent = on ? "正在生成中…" : "生成我的学习路径";
}

function showError(message) {
  const box = $("gen-error");
  box.hidden = false;
  box.textContent = message;
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
  term.appendChild(textEl("strong", "term-name", payload.term));
  term.appendChild(textEl("div", "term-def", payload.definition));
  if (payload.analogy) term.appendChild(textEl("div", "term-analogy", "类比：" + payload.analogy));
  if (payload.pitfalls) term.appendChild(textEl("div", "term-pitfalls", "注意：" + payload.pitfalls));
  card.appendChild(term);
}

function renderQuiz(payload) {
  const card = dayCard(payload.day);
  const quiz = textEl("div", "quiz-card", "");
  quiz.appendChild(textEl("div", "quiz-question", payload.question));
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
  done.appendChild(textEl("strong", "", "学习路径已生成完毕"));
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
    box.appendChild(textEl("h3", "", "配套开源项目（人工策展）"));
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
  if (!useSavedKeyChecked()) {
    // 未勾选代存：本机浏览器 Key 单次透传，服务端不落库
    const key = connectConfig().apiKey;
    if (key) headers["X-LLM-Key"] = key;
  }
  // 勾选代存时不发 X-LLM-Key，由服务端用加密代存的 Key 回答问题

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

/* ── M7.2：知识库检索与浏览 ───────────────────────────── */

let kbAbort = null; // 检索请求的 AbortController，快速连打时取消上一个

const KB_HINT_DEFAULT =
  "本地知识库即时检索 · 不消耗任何模型额度 · 结果带出处可核对";

function kbBlock(title) {
  const block = textEl("section", "kb-answer-block", "");
  block.appendChild(textEl("h3", "kb-block-title", title));
  return block;
}

/**
 * 渲染本地问答结果（零 Token）：
 * ① 用户句子里命中的名词解释 → ② 最相关的一个开源项目 → ③ 书中相关章节。
 */
function renderKbAnswer(data) {
  const box = $("kb-results");
  box.hidden = false;
  box.innerHTML = "";
  if (!data.total) {
    const empty = textEl("div", "kb-empty", "");
    const whale = document.createElement("img");
    whale.src = "/static/img/whale-xiaoyu-empty.png";
    whale.alt = "";
    whale.setAttribute("aria-hidden", "true");
    empty.appendChild(whale);
    empty.appendChild(
      textEl(
        "p",
        "muted",
        `本地知识库里暂时没有「${data.query}」的解释。换个说法试试，或点开下方章节浏览；接入自己的 API 后也可以用 AI 对话回答。`
      )
    );
    box.appendChild(empty);
    return;
  }
  box.appendChild(
    textEl(
      "div",
      "kb-result-count",
      "本地知识库直接回答（零 Token）：先看名词解释，再看一个最相关的开源项目"
    )
  );

  if (data.terms && data.terms.length) {
    const block = kbBlock("名词解释");
    const grid = textEl("div", "kb-term-grid", "");
    data.terms.forEach((item) => {
      const card = textEl("article", "kb-term-card", "");
      const label = textEl("span", "kb-card-label", "名词卡");
      label.dataset.kind = "term";
      card.appendChild(label);
      card.appendChild(textEl("strong", "kb-term-name", item.term));
      if (item.definition) card.appendChild(textEl("p", "kb-term-def", item.definition));
      if (item.analogy) card.appendChild(textEl("p", "kb-term-analogy", `类比：${item.analogy}`));
      if (item.pitfalls) card.appendChild(textEl("p", "kb-term-pitfall", `易错点：${item.pitfalls}`));
      card.appendChild(
        textEl("div", "meta", `出处：${item.source}` + (item.topic ? ` · ${item.topic}` : ""))
      );
      grid.appendChild(card);
    });
    block.appendChild(grid);
    box.appendChild(block);
  }

  if (data.link) {
    const block = kbBlock("最相关的开源项目");
    const card = textEl("article", "kb-link-card", "");
    const label = textEl("span", "kb-card-label", "开源项目");
    label.dataset.kind = "link";
    card.appendChild(label);
    card.appendChild(textEl("strong", "kb-link-title", data.link.title));
    if (data.link.why) card.appendChild(textEl("p", "why", data.link.why));
    card.appendChild(textEl("div", "meta", `主题：${data.link.topic} · 人工策展`));
    const a = document.createElement("a");
    a.href = data.link.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.className = "btn btn-ghost btn-small";
    a.textContent = "打开 GitHub →";
    card.appendChild(a);
    block.appendChild(card);
    box.appendChild(block);
  }

  if (data.chapters && data.chapters.length) {
    const block = kbBlock("书中相关章节");
    const list = textEl("ol", "kb-chapter-list", "");
    data.chapters.forEach((hit) => {
      const li = document.createElement("li");
      li.appendChild(textEl("strong", "kb-chapter-title", hit.title));
      li.appendChild(textEl("p", "kb-hit-snippet", hit.snippet));
      li.appendChild(
        textEl("div", "meta", `出处：${hit.source}` + (hit.topic ? ` · ${hit.topic}` : ""))
      );
      list.appendChild(li);
    });
    block.appendChild(list);
    box.appendChild(block);
  }
}

async function runKbSearch(query) {
  const trim = query.trim();
  const box = $("kb-results");
  if (!trim) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  $("kb-hint").textContent = "正在本地检索…";
  if (kbAbort) kbAbort.abort();
  kbAbort = new AbortController();
  try {
    // 本地问答：/api/kb/answer 一次返回名词卡 + 最相关链接 + 相关章节
    const res = await fetch(`/api/kb/answer?q=${encodeURIComponent(trim)}&limit=6`, {
      signal: kbAbort.signal,
    });
    const data = await res.json();
    if (!res.ok) {
      const msg = (data.error && data.error.message) || "检索失败，请稍后再试。";
      throw new Error(msg);
    }
    renderKbAnswer(data);
    $("kb-hint").textContent = KB_HINT_DEFAULT;
  } catch (err) {
    if (err.name === "AbortError") return; // 被新输入打断，忽略
    box.hidden = false;
    box.innerHTML = "";
    box.appendChild(textEl("p", "muted", err.message || "检索失败。"));
    $("kb-hint").textContent = KB_HINT_DEFAULT;
  }
}

function bindKbSearch() {
  const form = $("kb-form");
  if (!form) return;
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    runKbSearch($("kb-input").value);
  });
  // 输入停顿 350ms 后即时检索
  let timer = null;
  $("kb-input").addEventListener("input", (ev) => {
    clearTimeout(timer);
    const value = ev.target.value;
    timer = setTimeout(() => runKbSearch(value), 350);
  });
}

function renderBrowse(data) {
  const browse = $("browse-list");
  const topics = $("kb-topics");
  browse.innerHTML = "";
  topics.innerHTML = "";
  if (!data.chapters.length && !data.terms_topics.length) {
    browse.innerHTML = "<p class='muted'>知识库暂时为空。</p>";
    return;
  }
  // 主题导航：名词卡主题 → 直接检索；链接主题 → 跳到链接区按主题筛选
  const makeTopicChip = (label, kind) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "topic-chip";
    chip.dataset.kind = kind; // M8：用 CSS 小圆点区分 名词卡/链接 主题
    chip.textContent = label;
    chip.addEventListener("click", () => {
      if (kind === "term") {
        runKbSearch(label);
        $("kb-results").scrollIntoView({ behavior: "smooth", block: "start" });
      } else {
        loadLinks(label);
        $("links-list").scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
    return chip;
  };
  // 两组主题分开显示并加小标题，避免两排 chip 混在一起看起来像重复
  const addTopicGroup = (label, items, kind) => {
    if (!items || !items.length) return;
    const group = document.createElement("div");
    group.className = "browse-topic-group";
    const caption = document.createElement("p");
    caption.className = "browse-group-label";
    caption.textContent = label;
    group.appendChild(caption);
    const bar = document.createElement("div");
    bar.className = "topic-bar";
    items.forEach((t) => bar.appendChild(makeTopicChip(t, kind)));
    group.appendChild(bar);
    topics.appendChild(group);
  };
  addTopicGroup("名词卡主题（点开看概念解释）", data.terms_topics, "term");
  addTopicGroup("开源项目主题（按方向找项目）", data.links_topics, "link");

  // 章节目录：<details> 折叠每个章节小节
  data.chapters.forEach((ch) => {
    const item = document.createElement("details");
    item.className = "browse-chapter";
    const summary = document.createElement("summary");
    summary.textContent = ch.title || ch.file;
    item.appendChild(summary);
    if (ch.sections && ch.sections.length) {
      const ul = document.createElement("ul");
      ul.className = "browse-sections";
      ch.sections.forEach((s) => {
        const li = document.createElement("li");
        const a = document.createElement("button");
        a.type = "button";
        a.className = "browse-section-link";
        a.textContent = s;
        a.addEventListener("click", () => runKbSearch(s));
        li.appendChild(a);
        ul.appendChild(li);
      });
      item.appendChild(ul);
    }
    browse.appendChild(item);
  });
}

async function loadBrowse() {
  const browse = $("browse-list");
  if (!browse) return;
  browse.innerHTML = "<p class='muted'>加载中…</p>";
  try {
    const res = await fetch("/api/kb/overview");
    const data = await res.json();
    renderBrowse(data);
  } catch (err) {
    browse.innerHTML = "<p class='muted'>知识库加载失败，请刷新重试。</p>";
  }
}

/* ── 服务端代存状态（首页只读，写入/清除都在 /connect） ────── */

let vaultState = { enabled: false, reason: null, https: false, scopes: [] };

async function loadVaultState() {
  try {
    const res = await fetch(`/api/vault?client_id=${encodeURIComponent(ensureClientId())}`);
    vaultState = await res.json();
  } catch (err) {
    vaultState = { enabled: false, reason: null, https: false, scopes: [] };
  }
  syncSavedKeyCheckbox();
}

/** 生成学习路径时的「用服务器代存的 Key」勾选框：只在真的可用时放开。 */
function syncSavedKeyCheckbox() {
  const check = $("use-saved-key");
  if (!check) return;
  const usable = vaultState.enabled && vaultState.https && (vaultState.scopes || []).length > 0;
  check.disabled = !usable;
  if (!usable) check.checked = false;
}

/** 是否已经能调真实模型：本机 Key 或服务端代存二选一。 */
function modelAccess() {
  const cfg = connectConfig();
  if (cfg.apiKey) return { ready: true, useSavedKey: false, config: cfg };
  const scope = cfg.providerId || cfg.baseUrl;
  const saved = vaultState.enabled && vaultState.https && scope
    && (vaultState.scopes || []).indexOf(scope) >= 0;
  return { ready: !!saved, useSavedKey: !!saved, config: cfg };
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
  btn.textContent = label || "下载 Markdown";
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
    root.appendChild(textEl("h4", "detail-section", "配套开源项目（人工策展）"));
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
  if (!$("start-form")) {
    // 子页面（/kb、/connect）：由各分页自己的脚本接管，这里不碰首页元素
    return;
  }
  loadLinks("");
  bindForm();
  bindKbSearch();
  loadBrowse();
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

/* ── 鲸小鱼对话卡片（首页 #xiaoyu-card） ─────────────────────────
   深海鲸鱼娘「鲸小鱼」的角色对话：本地开场白 + POST /api/chat。
   · 所有文本用 textContent 写入，不做 innerHTML 拼接
   · 子页面没有 #xiaoyu-form 时整体跳过
   · mode=demo / scripted=true 时在气泡下方标注「预设台词」 */

const XIAOYU_OPENING = "哼，主人终于来了。（尾巴慢悠悠拍了一下）先说好，我才不是特意等你的。";
const XIAOYU_LOCKED_NOTE = "哼，主人还没给我接上模型，那我不说话。（尾巴一甩，缩回水里）";
const XIAOYU_SCRIPTED_NOTE = "预设台词 · 本站 Demo 模式暂不调用真实模型";
const XIAOYU_LIVE_NOTE = "已接入你的模型";
const XIAOYU_MAX_BUBBLES = 12;
const XIAOYU_TIMEOUT_MS = 30000;
let xiaoyuLiveNoted = false;

function xiaoyuEl(id) {
  return document.getElementById(id);
}

function xiaoyuGuardAvatar(img) {
  // 头像由另一位同学产出，文件还没到位时保留底框、不显示破图
  if (!img) return;
  img.addEventListener("error", function () {
    img.style.visibility = "hidden";
  }, { once: true });
}

function xiaoyuScrollToBottom(log) {
  if (!log) return;
  log.scrollTop = log.scrollHeight;
}

function xiaoyuTrim(log) {
  // 只保留最近 12 条气泡
  while (log.children.length > XIAOYU_MAX_BUBBLES) {
    log.removeChild(log.firstElementChild);
  }
}

function xiaoyuAvatarNode() {
  const holder = document.createElement("span");
  holder.className = "xiaoyu-msg-avatar";
  const img = document.createElement("img");
  img.className = "xiaoyu-msg-avatar-img";
  img.src = "/static/img/whale-xiaoyu-avatar.png";
  img.alt = "";
  img.width = 36;
  img.height = 36;
  xiaoyuGuardAvatar(img);
  holder.appendChild(img);
  return holder;
}

function xiaoyuAppendBubble(log, role, text, note, link) {
  if (!log) return null;
  const row = document.createElement("div");
  row.className = "xiaoyu-msg " + (role === "user" ? "is-user" : "is-bot");
  const main = document.createElement("div");
  main.className = "xiaoyu-msg-main";
  const bubble = document.createElement("div");
  bubble.className = "xiaoyu-bubble";
  bubble.textContent = text;
  main.appendChild(bubble);
  if (note) {
    const tip = document.createElement("p");
    tip.className = "xiaoyu-scripted";
    tip.textContent = note;
    main.appendChild(tip);
  }
  if (link) {
    // 链接也用 createElement + textContent 组装，不拼 innerHTML
    const a = document.createElement("a");
    a.className = "xiaoyu-link";
    a.href = link.href;
    a.textContent = link.label;
    main.appendChild(a);
  }
  if (role === "user") {
    row.appendChild(main);
  } else {
    row.appendChild(xiaoyuAvatarNode());
    row.appendChild(main);
  }
  log.appendChild(row);
  xiaoyuTrim(log);
  xiaoyuScrollToBottom(log);
  return row;
}

function xiaoyuErrorText(code, message) {
  if (code === "rate_limited") {
    return "主人说太快啦，等我喘口气（尾巴不满地拍水）";
  }
  if (message && message.trim()) return message.trim();
  return "（她鼓了一下腮帮子）好像出了点小岔子，主人待会儿再跟我说一次？";
}

function xiaoyuSetBusy(on) {
  const btn = xiaoyuEl("xiaoyu-send");
  const input = xiaoyuEl("xiaoyu-input");
  const form = xiaoyuEl("xiaoyu-form");
  if (btn) {
    btn.disabled = on;
    btn.textContent = on ? "说话中…" : "说话";
  }
  if (input) input.disabled = on;
  if (form) form.dataset.busy = on ? "1" : "";
}

function xiaoyuMarkLive() {
  if (xiaoyuLiveNoted) return;
  xiaoyuLiveNoted = true;
  const hint = xiaoyuEl("xiaoyu-hint");
  if (hint) hint.textContent = XIAOYU_LIVE_NOTE + "：现在的回答来自你配置的模型 API。";
}

async function xiaoyuSend(message) {
  const log = xiaoyuEl("xiaoyu-log");
  const input = xiaoyuEl("xiaoyu-input");
  if (!log) return;
  xiaoyuSetBusy(true);
  xiaoyuAppendBubble(log, "user", message, "");
  if (input) input.value = "";

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), XIAOYU_TIMEOUT_MS);
  try {
    let clientId = "c_guest";
    try {
      if (typeof ensureClientId === "function") clientId = ensureClientId();
    } catch (e) {
      clientId = "c_guest";
    }
    const access = modelAccess();
    if (!access.ready) {
      // 没接入任何模型就不发请求（用户要求：不接 API 不允许使用对话）
      xiaoyuAppendBubble(log, "bot", XIAOYU_LOCKED_NOTE, "", { href: "/connect", label: "去接入 API →" });
      return;
    }
    const headers = { "Content-Type": "application/json" };
    const cfg = access.config;
    if (!access.useSavedKey && cfg.apiKey) {
      // 本机浏览器 Key 单次透传（与生成学习路径一致，服务端不落库）
      headers["X-LLM-Key"] = cfg.apiKey;
    }
    // 使用服务端代存时不发 X-LLM-Key，由服务端解密代存的 Key
    const res = await fetch("/api/chat", {
      method: "POST",
      headers,
      body: JSON.stringify({
        client_id: clientId,
        message: message,
        provider_id: cfg.providerId || null,
        base_url: cfg.baseUrl || null,
        model: cfg.model || null,
        use_saved_key: access.useSavedKey,
      }),
      signal: controller.signal,
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = null;
    }
    if (!res.ok) {
      const err = (data && data.error) || {};
      let code = err.code || "";
      if (!code && res.status === 429) code = "rate_limited";
      if (!code && res.status === 400) code = "missing_key";
      if (code === "missing_key") {
        // 缺 Key：傲娇式提示 + 「去接入」链接
        xiaoyuAppendBubble(
          log,
          "bot",
          "哼，主人还没给我接上模型呢…（尾巴啪地拍了一下水）先把 Key 接上，我才能好好跟你说话。",
          "",
          { href: "/connect", label: "去接入自己的模型（/connect）" }
        );
        return;
      }
      xiaoyuAppendBubble(log, "bot", xiaoyuErrorText(code, err.message), "");
      return;
    }
    const reply = (data && typeof data.reply === "string" && data.reply.trim())
      ? data.reply.trim()
      : "（她把头埋进水里憋了半天）主人，我刚才好像走神了…再说一遍？";
    const scripted = !!(data && (data.scripted === true || data.mode === "demo"));
    xiaoyuAppendBubble(log, "bot", reply, scripted ? XIAOYU_SCRIPTED_NOTE : "");
    if (!scripted) xiaoyuMarkLive();
  } catch (err) {
    const aborted = err && (err.name === "AbortError" || err.name === "TimeoutError");
    xiaoyuAppendBubble(
      log,
      "bot",
      aborted
        ? "……等这么久还没回音（尾巴烦躁地拍了两下水面），主人再叫我一次？"
        : "（她甩了甩尾巴）网络好像闪了一下，主人再说一遍好不好？",
      ""
    );
  } finally {
    clearTimeout(timer);
    xiaoyuSetBusy(false);
  }
}

/** 按「有没有接入自己的模型」切换对话框两态：未接入只显示提示，不可输入。 */
function applyXiaoyuGate(access) {
  const card = xiaoyuEl("xiaoyu-card");
  const form = xiaoyuEl("xiaoyu-form");
  const locked = xiaoyuEl("xiaoyu-locked");
  const badge = xiaoyuEl("xiaoyu-state");
  const log = xiaoyuEl("xiaoyu-log");
  if (!card || !form || !locked) return;

  if (access.ready) {
    card.dataset.state = "ready";
    form.hidden = false;
    locked.hidden = true;
    if (badge) badge.textContent = access.useSavedKey ? "已连接 · 代存" : "已连接";
    if (log && !log.children.length) {
      // 本地开场白，不请求接口
      xiaoyuAppendBubble(log, "bot", XIAOYU_OPENING, "");
    }
    return;
  }

  card.dataset.state = "locked";
  form.hidden = true;
  locked.hidden = false;
  if (badge) badge.textContent = "未接入";
  if (log) log.textContent = "";
}

function xiaoyuInit() {
  const form = xiaoyuEl("xiaoyu-form");
  const log = xiaoyuEl("xiaoyu-log");
  const input = xiaoyuEl("xiaoyu-input");
  const send = xiaoyuEl("xiaoyu-send");
  if (!form || !log || !input || !send) return; // 子页面没有这张卡片：直接跳过

  xiaoyuGuardAvatar(document.querySelector("#xiaoyu-card .xiaoyu-avatar"));
  applyXiaoyuGate(modelAccess());

  form.addEventListener("submit", function (ev) {
    ev.preventDefault(); // Enter 与点按钮都走这里
    if (form.dataset.busy === "1") return;
    const text = input.value.trim();
    if (!text) {
      input.focus();
      return;
    }
    xiaoyuSend(text.slice(0, 1000));
  });
}

/** 先按本机配置渲染，再用服务端代存状态复核一次（代存可能已存在）。 */
async function xiaoyuBoot() {
  if (!xiaoyuEl("xiaoyu-card")) return;
  await loadVaultState();
  applyXiaoyuGate(modelAccess());
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    xiaoyuInit();
    xiaoyuBoot();
  });
} else {
  xiaoyuInit();
  xiaoyuBoot();
}