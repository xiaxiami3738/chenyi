/* /connect 页面专属脚本：供应商预设、本机 BYOK 保存、可选服务端加密代存。
   设计约定：
   - 所有文本用 textContent 写入，不拼 innerHTML，避免 XSS；
   - 永远不打印 Key 到 console，也不把 Key 拼进 URL；
   - 「测试连接」只做本地格式校验，绝不请求外部 API。
   用 IIFE 包裹，避免与全局 app.js 的函数名冲突。 */
(function () {
  "use strict";

  // 本页专用的 localStorage 键（服务端无关，清除浏览器数据即消失）
  const LS = {
    provider: "chenyi.connect.provider",
    model: "chenyi.connect.model",
    baseUrl: "chenyi.connect.baseUrl",
    apiKey: "chenyi.connect.apiKey",
    clientId: "chenyi.clientId", // 与主站 app.js 共用同一个匿名标识
  };

  const $ = (id) => document.getElementById(id);

  // 中文状态文案（形如 终端提示行），统一从这里取，避免散落
  const STATUS = {
    idle: "⚠ 未配置 · 先选供应商并填写 Key",
    ready: "✓ 已就绪 · Key 只在本机浏览器，未上传",
    testing: "› 正在本地校验…",
    testFail: "⚠ 校验未通过 · 检查下面提示",
    saved: "✓ 已保存在本机浏览器",
  };

  /** 生成 / 复用匿名 client_id（与主站规则一致："c_" + 随机串 + 时间戳 36 进制）。 */
  function ensureClientId() {
    let id = localStorage.getItem(LS.clientId);
    if (!id) {
      id = "c_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(LS.clientId, id);
    }
    return id;
  }

  /** 底部状态行：ok / warn / info 三种语气，纯文字符号，不用 emoji。 */
  function setStatus(kind, text) {
    const el = $("cx-status");
    if (!el) return;
    el.textContent = text;
    el.className = "cx-status" + (kind ? " " + kind : "");
  }

  /** 小提示区：写入文字并切换 ok / warn 语气（aria-live 已被 HTML 声明）。 */
  function setHint(id, text, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.classList.remove("cx-ok", "cx-warn");
    if (kind) el.classList.add(kind === "ok" ? "cx-ok" : "cx-warn");
  }

  /** 当前选中的供应商预设对象（来自 /api/providers）。 */
  let presets = [];
  function currentPreset() {
    const id = $("cx-provider").value;
    return presets.find((p) => p.id === id) || null;
  }

  /** 终端区块里的实时预览（Key 只显示是否已填，绝不显示内容）。 */
  function updateTerminal() {
    const preset = currentPreset();
    const model = $("cx-model").value.trim();
    const baseUrl = $("cx-base-url").value.trim();
    const hasKey = $("cx-api-key").value.trim().length > 0;

    $("cx-t-provider").textContent = preset ? preset.id : "未选择";
    $("cx-t-model").textContent = model || "—";
    $("cx-t-base-url").textContent = baseUrl || "—";
    $("cx-t-key").textContent = hasKey ? "已填写（仅本机可见）" : "未填写";
  }

  /** 用校验结果刷新底部状态行。 */
  function refreshStatus() {
    const hasProvider = Boolean($("cx-provider").value);
    const hasKey = $("cx-api-key").value.trim().length > 0;
    if (hasProvider && hasKey) {
      setStatus("ok", STATUS.ready);
    } else {
      setStatus("warn", STATUS.idle);
    }
    updateTerminal();
  }

  // ── 供应商列表 ───────────────────────────────────────────────
  async function loadProviders() {
    const select = $("cx-provider");
    try {
      const res = await fetch("/api/providers");
      const data = await res.json();
      presets = Array.isArray(data.providers) ? data.providers : [];

      select.textContent = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "请选择一家供应商…";
      select.appendChild(placeholder);

      const datalist = $("cx-model-options");
      datalist.textContent = "";
      presets.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.label;
        opt.dataset.baseUrl = p.base_url || "";
        opt.dataset.model = p.default_model || "";
        select.appendChild(opt);

        (p.models || []).forEach((m) => {
          const item = document.createElement("option");
          item.value = m;
          datalist.appendChild(item);
        });
      });

      // 恢复上次选中的供应商，再回填默认模型 / Base URL
      const savedProvider = localStorage.getItem(LS.provider) || "";
      if (savedProvider && presets.some((p) => p.id === savedProvider)) {
        select.value = savedProvider;
      }
      if (data.usage) setHint("cx-usage", data.usage);
      else if (presets.length) setHint("cx-usage", "供应商预设来自站点后端，选中后会自动带出默认模型与 Base URL，随时可以手动改。");

      applyPresetDefaults(false);
      restoreLocalFields();
      refreshStatus();
    } catch (err) {
      select.textContent = "";
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "无法加载供应商列表，请刷新重试";
      select.appendChild(opt);
      setStatus("warn", "⚠ 无法加载供应商列表 · 请检查服务是否在运行");
    }
  }

  /** 选中供应商后带出默认模型 / Base URL；只在用户主动切换时覆盖输入框。 */
  function applyPresetDefaults(overwrite) {
    const preset = currentPreset();
    const note = $("cx-provider-note");
    if (!preset) {
      note.textContent = "选供应商后自动填写②③，可手动覆盖。";
      updateTerminal();
      return;
    }
    const modelInput = $("cx-model");
    const baseInput = $("cx-base-url");
    if (overwrite || !modelInput.value.trim()) modelInput.value = preset.default_model || "";
    if (overwrite || !baseInput.value.trim()) baseInput.value = preset.base_url || "";
    modelInput.placeholder = preset.default_model ? "例如 " + preset.default_model : "例如 model-name";

    // 供应商备注（官网申请入口）用纯文本展示，避免把后端文案当 HTML
    const parts = [preset.label];
    if (preset.note) parts.push(preset.note);
    if (preset.docs) parts.push("申请入口：" + preset.docs);
    note.textContent = parts.join(" · ");
    updateTerminal();
  }

  // ── 本机 BYOK 保存 / 读取 / 清除 ─────────────────────────────
  function restoreLocalFields() {
    const provider = localStorage.getItem(LS.provider);
    const model = localStorage.getItem(LS.model);
    const baseUrl = localStorage.getItem(LS.baseUrl);
    const apiKey = localStorage.getItem(LS.apiKey);

    if (provider) $("cx-provider").value = provider;
    if (model) $("cx-model").value = model;
    if (baseUrl) $("cx-base-url").value = baseUrl;
    if (apiKey) $("cx-api-key").value = apiKey;

    const hasAny = Boolean(provider || model || baseUrl || apiKey);
    if (hasAny) {
      setHint("cx-local-status", "已从本机浏览器读取上次保存的配置（未上传）", "ok");
    }
    applyPresetDefaults(false);
  }

  /** 本地校验：非空 / http(s) 前缀 / Key 长度 >= 8，纯本地、零网络。 */
  function validateLocal() {
    const provider = $("cx-provider").value;
    const model = $("cx-model").value.trim();
    const baseUrl = $("cx-base-url").value.trim();
    const apiKey = $("cx-api-key").value.trim();

    if (!provider) return "请先选择供应商（①）。";
    if (!model) return "模型名不能为空（②）。";
    if (!/^https?:\/\//i.test(baseUrl)) return "Base URL 必须以 http:// 或 https:// 开头（③）。";
    if (apiKey.length < 8) return "API Key 长度至少 8 位（④），请检查是否复制完整。";
    return "";
  }

  function saveLocal() {
    const error = validateLocal();
    if (error) {
      setHint("cx-local-status", "⚠ " + error, "warn");
      setHint("cx-test-result", "⚠ 未通过本地校验，未保存。", "warn");
      setStatus("warn", STATUS.testFail);
      return;
    }
    localStorage.setItem(LS.provider, $("cx-provider").value);
    localStorage.setItem(LS.model, $("cx-model").value.trim());
    localStorage.setItem(LS.baseUrl, $("cx-base-url").value.trim());
    localStorage.setItem(LS.apiKey, $("cx-api-key").value.trim());

    setHint("cx-local-status", "✓ 已保存在本机浏览器（不会自动上传）", "ok");
    setHint("cx-test-result", "✓ 格式校验通过，可以开始生成学习路径。", "ok");
    setStatus("ok", STATUS.saved);
    updateTerminal();
  }

  function clearLocal() {
    Object.keys(LS).forEach((key) => {
      if (key !== "clientId") localStorage.removeItem(LS[key]);
    });
    // 表单一起回到空白状态，避免「已清除」但字段仍留着上次内容造成误会
    $("cx-provider").value = "";
    $("cx-model").value = "";
    $("cx-base-url").value = "";
    $("cx-api-key").value = "";
    setHint("cx-local-status", "✓ 已清除本机保存的配置", "ok");
    setHint("cx-test-result", "已清除，可重新填写。");
    applyPresetDefaults(false);
    refreshStatus();
  }

  // ── 可选：服务端加密代存 ─────────────────────────────────────
  let vaultState = { enabled: false, reason: null, https: false, scopes: [] };

  function vaultReasonText(reason) {
    if (reason === "demo_mode") {
      return "Demo 模式不调用真实模型，代存没有意义；站点开启真实模式（APP_MODE=live）后才可用。";
    }
    if (reason === "vault_not_configured") {
      return "服务器未配置主密钥（VAULT_MASTER_KEY），代存功能未启用。";
    }
    return "服务器代存当前不可用。";
  }

  async function loadVaultState() {
    const clientId = ensureClientId();
    $("cx-client-id").textContent = clientId;
    try {
      const res = await fetch("/api/vault?client_id=" + encodeURIComponent(clientId));
      const data = await res.json();
      vaultState = {
        enabled: Boolean(data.enabled),
        reason: data.reason || null,
        https: Boolean(data.https),
        scopes: Array.isArray(data.scopes) ? data.scopes : [],
      };
    } catch (err) {
      vaultState = { enabled: false, reason: "network", https: false, scopes: [] };
    }
    renderVaultState();
  }

  function renderVaultState() {
    const useBox = $("cx-vault-use");
    const saveBtn = $("cx-save-vault");
    const clearBtn = $("cx-clear-vault");
    const note = $("cx-vault-note");
    renderVaultScopes();

    if (vaultState.enabled) {
      note.textContent = vaultState.https
        ? "代存可用：勾选后 Key 会用服务器主密钥加密保存，之后可免填生成（可随时删除）。"
        : "代存已开启，但当前不是 HTTPS 连接：写操作会被服务端拒绝（403），请在 HTTPS 或本地受信环境下使用。";
      useBox.disabled = false;
      saveBtn.disabled = false;
      clearBtn.disabled = false;
    } else {
      note.textContent = vaultReasonText(vaultState.reason);
      useBox.disabled = true;
      useBox.checked = false;
      saveBtn.disabled = true;
      clearBtn.disabled = true;
    }
  }

  /** 代存 scope：默认取供应商 id；Base URL 被改过时退回自定义 base_url。 */
  function currentScope() {
    const provider = $("cx-provider").value;
    const preset = currentPreset();
    const baseUrl = $("cx-base-url").value.trim();
    if (!provider) return baseUrl;
    if (baseUrl && preset && baseUrl !== preset.base_url) return baseUrl;
    return provider;
  }

  function renderVaultScopes() {
    const box = $("cx-vault-scopes");
    box.textContent = "";
    const scopes = vaultState.scopes || [];
    box.hidden = scopes.length === 0;
    scopes.forEach((scope) => {
      const chip = document.createElement("span");
      chip.className = "scope-chip";
      chip.textContent = "已代存 · " + scope;
      box.appendChild(chip);
    });
  }

  /** 统一解析响应：出错时优先展示后端返回的 error.message。 */
  async function readJson(res) {
    let data = null;
    try {
      data = await res.json();
    } catch (err) {
      data = null;
    }
    if (!res.ok) {
      const message =
        (data && data.error && data.error.message) ||
        (data && data.detail) ||
        "服务端返回 " + res.status;
      throw new Error(String(message));
    }
    return data || {};
  }

  async function saveVault() {
    if (!$("cx-vault-use").checked) {
      setHint("cx-vault-note", "请先勾选上方的「我同意把 Key 加密保存到服务器」。", "warn");
      return;
    }
    const error = validateLocal();
    if (error) {
      setHint("cx-vault-note", "⚠ " + error, "warn");
      return;
    }
    const apiKey = $("cx-api-key").value.trim();
    const scope = currentScope();
    const saveBtn = $("cx-save-vault");
    saveBtn.disabled = true;
    try {
      const res = await fetch("/api/vault", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: ensureClientId(), scope: scope, api_key: apiKey }),
      });
      await readJson(res);
      setHint("cx-vault-note", "✓ 已加密保存到服务器（scope：" + scope + "）", "ok");
      // 保存成功后立刻刷新 scope 列表（GET 只返回 scope，不返回 Key）
      vaultState.scopes = Array.from(new Set(vaultState.scopes.concat(scope))).sort();
      renderVaultScopes();
      setStatus("ok", "✓ 已加密保存到服务器 · 之后可免填生成");
    } catch (err) {
      setHint("cx-vault-note", "⚠ " + err.message, "warn");
      setStatus("warn", "⚠ 代存失败 · " + err.message);
    } finally {
      saveBtn.disabled = false;
    }
  }

  async function clearVault() {
    const scope = currentScope();
    if (!scope) {
      setHint("cx-vault-note", "当前没有可删除的 scope（先选供应商或填 Base URL）。", "warn");
      return;
    }
    const clearBtn = $("cx-clear-vault");
    clearBtn.disabled = true;
    try {
      // 注意：DELETE 必须带 body，服务端用 Body 读取 client_id / scope
      const res = await fetch("/api/vault", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: ensureClientId(), scope: scope }),
      });
      const data = await readJson(res);
      vaultState.scopes = vaultState.scopes.filter((s) => s !== scope);
      renderVaultScopes();
      setHint(
        "cx-vault-note",
        data.deleted ? "✓ 已删除服务器上的密文（scope：" + scope + "）" : "服务器上本来就没有这个 scope 的密文。",
        "ok"
      );
      setStatus("ok", data.deleted ? "✓ 已删除服务器上的密文" : "✓ 没有可删除的密文");
    } catch (err) {
      setHint("cx-vault-note", "⚠ " + err.message, "warn");
      setStatus("warn", "⚠ 删除失败 · " + err.message);
    } finally {
      clearBtn.disabled = false;
    }
  }

  // ── 事件绑定与初始化 ─────────────────────────────────────────
  function bind() {
    $("cx-provider").addEventListener("change", () => {
      applyPresetDefaults(true);
      refreshStatus();
    });
    ["cx-model", "cx-base-url", "cx-api-key"].forEach((id) => {
      $(id).addEventListener("input", () => {
        refreshStatus();
      });
    });

    $("cx-test").addEventListener("click", () => {
      setHint("cx-test-result", "› 正在本地校验…");
      setStatus("info", STATUS.testing);
      const error = validateLocal();
      if (error) {
        setHint("cx-test-result", "⚠ " + error, "warn");
        setStatus("warn", STATUS.testFail);
      } else {
        setHint("cx-test-result", "✓ 本地校验通过（未发起网络请求，Key 没有被发送）。", "ok");
        setStatus("ok", STATUS.ready);
      }
    });

    $("cx-save-local").addEventListener("click", saveLocal);
    $("cx-clear-local").addEventListener("click", clearLocal);
    $("cx-save-vault").addEventListener("click", saveVault);
    $("cx-clear-vault").addEventListener("click", clearVault);

    $("cx-vault-use").addEventListener("change", () => {
      const checked = $("cx-vault-use").checked;
      setStatus(checked ? "info" : "warn", checked ? "› 已勾选代存 · 点「加密保存到服务器」才会发送 Key" : STATUS.idle);
    });
  }

  function init() {
    ensureClientId();
    bind();
    loadProviders();
    loadVaultState();
    setStatus("warn", STATUS.idle);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();