/* 前端脚本：只做三件事 —— 展示状态、在本机保存 BYOK 配置、渲染策展链接。
   注意：M0 阶段还没有生成流水线，Key 不会被发送到任何地方。 */

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
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const payload = {
      client_id: ensureClientId(),
      goal: $("goal").value.trim(),
      level: $("level").value,
      weak_points: $("weak_points").value.trim(),
      daily_minutes: Number($("daily_minutes").value || 30),
    };
    if (!payload.goal) {
      $("start-hint").textContent = "请先写下你的目标。";
      return;
    }
    const box = $("start-result");
    box.hidden = false;
    box.innerHTML =
      "<strong>已记录你的输入（本机）：</strong><pre>" +
      JSON.stringify(payload, null, 2).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c])) +
      "</pre><p class='hint'>生成流水线（多 Agent 编排 + SSE 实时进度）将在 M1/M2 接入；" +
      "当前可用的是策展链接与供应商预设。</p>";
  });
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