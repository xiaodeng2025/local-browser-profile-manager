(() => {
  let apiBase = "";
  let settings = {};
  let createBusy = false;
  const profileBusy = new Map();
  const pageCache = new Map();
  const shortcutsCache = new Map();
  const statusEl = document.querySelector("#service-status");
  const profilesEl = document.querySelector("#profiles");
  const template = document.querySelector("#profile-template");
  const createForm = document.querySelector("#create-form");
  const createButton = document.querySelector("#create-profile-button");

  const BUSY_LABELS = {
    start: "启动中…",
    refresh: "刷新中…",
    stop: "停止中…",
    "open-shortcut": "打开中…",
    "save-settings": "保存中…",
  };

  const message = (text, kind = "") => { statusEl.textContent = text; statusEl.className = `service-status ${kind}`; };
  const request = async (base, path, options = {}) => {
    const response = await fetch(`${base}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
    return payload;
  };
  const api = (path, options) => request(apiBase, path, options);
  const ui = (path, options) => request("", path, options);

  function rememberLabel(button) {
    if (!button.dataset.defaultLabel) button.dataset.defaultLabel = button.textContent;
  }

  function defaultButtonDisabled(button, profile) {
    const action = button.dataset.action;
    const running = profile.status === "running";
    if (action === "start") return running || profile.status === "starting" || profile.status === "stopping";
    if (["refresh", "stop", "open-shortcut"].includes(action)) return !running;
    return false;
  }

  function setButtons(card, profile) {
    const busyAction = profileBusy.get(profile.id);
    card.setAttribute("aria-busy", busyAction ? "true" : "false");
    card.querySelectorAll("button").forEach((button) => {
      rememberLabel(button);
      const action = button.dataset.action;
      button.disabled = Boolean(busyAction) || defaultButtonDisabled(button, profile);
      button.setAttribute("aria-busy", busyAction && action === busyAction ? "true" : "false");
      button.textContent = busyAction && action === busyAction ? BUSY_LABELS[busyAction] : button.dataset.defaultLabel;
    });
  }

  async function refreshPages(profile) {
    if (profile.status !== "running") return [];
    const result = await api(`/api/profiles/${encodeURIComponent(profile.id)}/pages`);
    pageCache.set(profile.id, result.pages || []);
    return result.pages || [];
  }

  function renderPages(card, pages) {
    const list = card.querySelector(".page-list");
    const empty = card.querySelector(".page-empty");
    list.replaceChildren();
    empty.hidden = pages.length > 0;
    pages.forEach((page) => {
      const li = document.createElement("li");
      const id = document.createElement("code"); id.textContent = page.page_id;
      const title = document.createElement("strong"); title.textContent = page.title || "（无标题）";
      const url = document.createElement("span"); url.textContent = page.url || "about:blank";
      li.append(id, title, url); list.append(li);
    });
  }

  async function runProfileAction(profile, action, card, work, successText) {
    if (profileBusy.has(profile.id)) return null;
    profileBusy.set(profile.id, action);
    setButtons(card, profile);
    message(`正在${BUSY_LABELS[action].replace("…", "")} ${profile.id}…`, "ok");
    try {
      const result = await work();
      profileBusy.delete(profile.id);
      setButtons(card, profile);
      await refresh({ silent: true });
      message(typeof successText === "function" ? successText(result) : successText, "ok");
      return result;
    } catch (error) {
      profileBusy.delete(profile.id);
      setButtons(card, profile);
      try { await refresh({ silent: true }); } catch { /* Preserve the action error. */ }
      message(`${BUSY_LABELS[action].replace("…", "")}失败：${error.message}`, "error");
      return null;
    }
  }

  function renderShortcuts(card, profile) {
    const list = card.querySelector(".shortcut-list"); list.replaceChildren();
    const shortcuts = shortcutsCache.get(profile.id) || [];
    shortcuts.forEach((shortcut, index) => {
      const li = document.createElement("li");
      const info = document.createElement("span"); info.className = "shortcut-info";
      const name = document.createElement("strong"); name.textContent = shortcut.name;
      const url = document.createElement("code"); url.textContent = shortcut.url;
      info.append(name, url);
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "在此档案打开";
      open.dataset.action = "open-shortcut";
      open.addEventListener("click", async () => {
        if (profile.status !== "running") { message("档案尚未运行；请先手动启动后再打开快捷入口。", "error"); return; }
        await runProfileAction(
          profile,
          "open-shortcut",
          card,
          () => api(`/api/profiles/${encodeURIComponent(profile.id)}/pages`, { method: "POST", body: JSON.stringify({ url: shortcut.url }) }),
          (created) => `已在 ${profile.id} 新建 ${created.page.page_id}。`,
        );
      });
      const remove = document.createElement("button");
      remove.type = "button"; remove.textContent = "移除"; remove.dataset.action = "remove-shortcut";
      remove.addEventListener("click", () => {
        if (profileBusy.has(profile.id)) return;
        shortcuts.splice(index, 1); shortcutsCache.set(profile.id, shortcuts); renderShortcuts(card, profile); setButtons(card, profile);
      });
      li.append(info, open, remove); list.append(li);
    });
  }

  function networkLabel(network) {
    if (!network || network.mode !== "fixed") return "直连";
    return `固定代理 · ${(network.scheme || "").toUpperCase()}${network.authentication === "basic" ? " · 账号认证" : ""}`;
  }

  function fingerprintLabel(fingerprint) {
    if (!fingerprint || fingerprint.mode !== "fixed") return "未启用";
    return fingerprint.engine === "locked" ? "固定 / 浏览器版本已锁定" : "固定 / 待首次启动锁定";
  }

  function renderProfile(profile) {
    const meta = settings[profile.id] || { display_name: profile.name || profile.id, color: "#2563eb", note: "", shortcuts: [] };
    const network = profile.network || { mode: "direct" };
    const fragment = template.content.cloneNode(true);
    const card = fragment.querySelector(".profile-card");
    card.dataset.profileId = profile.id;
    card.querySelector(".color-dot").style.background = meta.color;
    card.querySelector(".profile-name").textContent = meta.display_name;
    card.querySelector(".profile-id").textContent = profile.id;
    const status = card.querySelector(".status"); status.textContent = profile.status; status.classList.add(profile.status);
    card.querySelector(".profile-note").textContent = meta.note || "暂无备注";
    const pages = pageCache.get(profile.id) || [];
    card.querySelector(".page-count").textContent = `${pages.length} 个`;
    card.querySelector(".network-status").textContent = networkLabel(network);
    card.querySelector(".fingerprint-status").textContent = fingerprintLabel(profile.fingerprint);
    renderPages(card, pages);
    const form = card.querySelector(".settings-form");
    form.display_name.value = meta.display_name; form.color.value = meta.color; form.note.value = meta.note || "";
    form.network_mode.value = network.mode;
    form.proxy_scheme.value = network.scheme || "http";
    form.proxy_host.value = network.host || "";
    form.proxy_port.value = network.port || "";
    form.proxy_authentication.value = network.authentication || "none";
    form.proxy_username.value = "";
    form.proxy_password.value = "";
    form.dataset.network = JSON.stringify(network);
    const syncNetworkFields = () => {
      const fixed = form.network_mode.value === "fixed";
      const stopped = profile.status === "stopped";
      const socks5 = form.proxy_scheme.value === "socks5";
      if (socks5 && form.proxy_authentication.value === "basic") form.proxy_authentication.value = "none";
      const basic = fixed && form.proxy_authentication.value === "basic";
      form.querySelector(".proxy-fields").hidden = !fixed;
      form.querySelector(".proxy-credential-fields").hidden = !basic;
      form.querySelector(".network-running").hidden = stopped;
      form.network_mode.disabled = !stopped;
      [form.proxy_scheme, form.proxy_host, form.proxy_port, form.proxy_authentication].forEach((field) => { field.disabled = !stopped || !fixed; });
      form.proxy_authentication.querySelector('option[value="basic"]').disabled = socks5;
      [form.proxy_username, form.proxy_password].forEach((field) => { field.disabled = !stopped || !basic; });
      form.proxy_host.required = fixed;
      form.proxy_port.required = fixed;
      form.proxy_username.required = basic;
      form.proxy_password.required = basic;
    };
    const networkPayload = () => form.network_mode.value === "direct"
      ? { mode: "direct" }
      : { mode: "fixed", scheme: form.proxy_scheme.value, host: form.proxy_host.value.trim(), port: form.proxy_port.valueAsNumber, authentication: form.proxy_authentication.value };
    form.network_mode.addEventListener("change", syncNetworkFields);
    form.proxy_scheme.addEventListener("change", syncNetworkFields);
    form.proxy_authentication.addEventListener("change", syncNetworkFields);
    syncNetworkFields();
    shortcutsCache.set(profile.id, [...(meta.shortcuts || [])]); renderShortcuts(card, profile);

    card.querySelector('[data-action="start"]').addEventListener("click", () => act(profile, "start", card));
    card.querySelector('[data-action="refresh"]').addEventListener("click", () => act(profile, "refresh", card));
    card.querySelector('[data-action="stop"]').addEventListener("click", () => act(profile, "stop", card));
    card.querySelector('[data-action="add-shortcut"]').addEventListener("click", () => {
      if (profileBusy.has(profile.id)) return;
      const name = form.shortcut_name.value.trim(); const url = form.shortcut_url.value.trim();
      if (!name || !/^https?:\/\//i.test(url)) { message("快捷入口需要平台名和 http/https 地址。", "error"); return; }
      const values = shortcutsCache.get(profile.id) || [];
      values.push({ name, url }); shortcutsCache.set(profile.id, values);
      form.shortcut_name.value = ""; form.shortcut_url.value = ""; renderShortcuts(card, profile); setButtons(card, profile);
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = () => ({ display_name: form.display_name.value, color: form.color.value, note: form.note.value, shortcuts: shortcutsCache.get(profile.id) || [] });
      await runProfileAction(
        profile,
        "save-settings",
        card,
        async () => {
          const desiredNetwork = networkPayload();
          const originalNetwork = JSON.parse(form.dataset.network || '{"mode":"direct"}');
          if (JSON.stringify(desiredNetwork) !== JSON.stringify(originalNetwork)) {
            if (profile.status !== "stopped") throw new Error("请先停止档案，再修改网络出口。");
            const networkRequest = desiredNetwork.authentication === "basic"
              ? { ...desiredNetwork, username: form.proxy_username.value, password: form.proxy_password.value }
              : desiredNetwork;
            await api(`/api/profiles/${encodeURIComponent(profile.id)}/network`, { method: "PUT", body: JSON.stringify(networkRequest) });
          }
          const saved = await ui(`/api/ui/settings/${encodeURIComponent(profile.id)}`, { method: "PUT", body: JSON.stringify(payload()) });
          settings[profile.id] = saved.settings;
          return saved;
        },
        `已保存 ${profile.id} 的本机设置；网络出口将在下次启动时生效。`,
      );
    });
    setButtons(card, profile);
    return fragment;
  }

  async function act(profile, action, card) {
    const success = { start: "浏览器档案已启动。", refresh: "标签页已刷新。", stop: "浏览器档案已停止。" };
    await runProfileAction(profile, action, card, async () => {
      if (action === "start") return api(`/api/profiles/${encodeURIComponent(profile.id)}/start`, { method: "POST", body: "{}" });
      if (action === "stop") return api(`/api/profiles/${encodeURIComponent(profile.id)}/stop`, { method: "POST", body: "{}" });
      return refreshPages({ id: profile.id, status: "running" });
    }, success[action]);
  }

  async function refresh({ silent = false } = {}) {
    try {
      const [health, profileResult, settingResult] = await Promise.all([api("/api/health"), api("/api/profiles"), ui("/api/ui/settings")]);
      settings = settingResult.profiles || {};
      await Promise.all(profileResult.profiles.filter((profile) => profile.status === "running").map(async (profile) => {
        try { await refreshPages(profile); } catch { pageCache.set(profile.id, []); }
      }));
      profilesEl.replaceChildren(...profileResult.profiles.map(renderProfile));
      if (!silent) message(`本地服务正常 · 运行中 ${health.running_profile_count} 个档案 · 并发上限 ${health.soft_concurrency_limit}`, "ok");
    } catch (error) { message(`无法连接本地服务：${error.message}`, "error"); }
  }

  function setCreateBusy(busy) {
    createBusy = busy;
    createForm.setAttribute("aria-busy", busy ? "true" : "false");
    createButton.disabled = busy;
    createButton.setAttribute("aria-busy", busy ? "true" : "false");
    createButton.textContent = busy ? "创建中…" : "新增档案";
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (createBusy) return;
    const input = document.querySelector("#new-profile-name");
    setCreateBusy(true); message("正在创建新的浏览器档案…", "ok");
    try {
      await api("/api/profiles", { method: "POST", body: JSON.stringify({ name: input.value }) });
      input.value = ""; await refresh({ silent: true }); message("已创建停止状态的新浏览器档案。", "ok");
    } catch (error) { message(`新增失败：${error.message}`, "error"); }
    finally { setCreateBusy(false); }
  });

  (async () => { const config = await ui("/ui-config.json"); apiBase = config.api_base; await refresh(); })().catch((error) => message(`页面初始化失败：${error.message}`, "error"));
})();
