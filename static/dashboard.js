(() => {
  "use strict";

  const state = {
    page: "overview",
    token: sessionStorage.getItem("bot-dashboard-token") || "",
    guildId: localStorage.getItem("bot-dashboard-guild") || "",
    userId: localStorage.getItem("bot-dashboard-user") || "",
    wishlist: [],
    flights: [],
    selectedWishlist: null,
    selectedFlight: null,
    ranges: { wishlist: 30, flight: 90 },
    analytics: null,
    requestVersions: new Map(),
    statsTimer: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
  const idValue = (value, name = "ID") => {
    const text = String(value || "").trim();
    if (!/^\d+$/.test(text)) throw new Error(`${name} must contain digits only.`);
    return text;
  };
  const unixSeconds = (localDateTime) => {
    const timestamp = new Date(localDateTime).getTime();
    if (!Number.isFinite(timestamp)) throw new Error("Choose a valid date and time.");
    return timestamp / 1000;
  };
  const formatDate = (seconds, withTime = true) => {
    if (seconds === null || seconds === undefined || seconds === "") return "Never";
    const date = new Date(Number(seconds) * 1000);
    if (Number.isNaN(date.getTime())) return "Unknown";
    return withTime ? date.toLocaleString() : date.toLocaleDateString();
  };
  const formatBytes = (bytes) => {
    if (bytes === null || bytes === undefined) return "Unavailable";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = Number(bytes); let index = 0;
    while (Math.abs(value) >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return `${value.toFixed(index > 2 ? 1 : 0)} ${units[index]}`;
  };
  const formatUptime = (seconds) => {
    if (seconds === null || seconds === undefined) return "Unavailable";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${days}d ${hours}h ${minutes}m`;
  };
  const formatPrice = (value, currency = "") => value === null || value === undefined
    ? "—" : `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${escapeHtml(currency)}`.trim();
  const hostname = (value) => { try { return new URL(value).hostname; } catch (_error) { return value; } };

  function version(key) {
    const next = (state.requestVersions.get(key) || 0) + 1;
    state.requestVersions.set(key, next);
    return { key, value: next, current: () => state.requestVersions.get(key) === next };
  }

  async function api(path, options = {}) {
    const headers = { "X-Discord-ID-Format": "string", ...(options.headers || {}) };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { ...options, headers });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (response.status === 401) {
      state.token = "";
      sessionStorage.removeItem("bot-dashboard-token");
      showLogin("That API token was not accepted. Try again.");
    }
    if (!response.ok) {
      const message = payload?.detail ? `${payload.error}: ${payload.detail}` : payload?.error;
      throw new Error(message || `Request failed (${response.status})`);
    }
    return payload;
  }

  function showLogin(message = "") {
    clearInterval(state.statsTimer); state.statsTimer = null;
    $("#login-error").textContent = message;
    $("#auth-overlay").classList.remove("hidden");
    $("[name='token']", $("#login-form")).focus();
  }

  function hideLogin() {
    $("#login-error").textContent = "";
    $("#auth-overlay").classList.add("hidden");
  }

  async function login(token) {
    state.token = String(token || "").trim();
    if (!state.token) throw new Error("Enter your API token.");
    await api("/system/stats");
    sessionStorage.setItem("bot-dashboard-token", state.token);
    hideLogin();
    navigate(state.page);
  }

  function toast(message, kind = "success") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    $("#toast-region").append(node);
    setTimeout(() => node.remove(), 4200);
  }

  async function submit(form, task, successMessage) {
    const button = $("button[type='submit'], button:not([type])", form);
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try {
      await task(new FormData(form));
      if (successMessage) toast(successMessage);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  }

  function requireGuild() { return idValue(state.guildId, "Server ID"); }
  function requireUser() { return idValue(state.userId, "User ID"); }
  function query(params) { return new URLSearchParams(params).toString(); }

  function renderRankings(target, rows) {
    if (!rows?.length) { target.innerHTML = '<div class="empty">No usage has been recorded for this scope.</div>'; return; }
    const max = Math.max(...rows.map((row) => row.count), 1);
    target.innerHTML = `<div class="rank-list">${rows.map((row) => `<div class="rank-row"><span>${escapeHtml(row.keyword)}</span><div class="rank-track"><i style="width:${Math.max(4, row.count / max * 100)}%"></i></div><strong>${row.count}</strong></div>`).join("")}</div>`;
  }

  function renderFeedback(target, groups) {
    if (!groups?.length) { target.innerHTML = '<div class="empty">No rated replies for this server yet.</div>'; return; }
    target.innerHTML = `<table class="data-table"><thead><tr><th>Group</th><th>Ratings</th><th>Approval</th><th>Compare</th></tr></thead><tbody>${groups.map((group) => `<tr><td>${escapeHtml(group.category)}<span class="cell-muted">${escapeHtml(group.model)} · ${escapeHtml(group.prompt_version)}</span></td><td>${group.ratings}<span class="cell-muted">${group.up} up · ${group.down} down</span></td><td>${group.approval_percent}%</td><td><span class="tag ${group.ready_to_compare ? "good" : ""}">${group.ready_to_compare ? "ready" : `${10 - group.ratings} needed`}</span></td></tr>`).join("")}</tbody></table>`;
  }

  async function loadStats() {
    const ticket = version("stats");
    try {
      const data = await api("/system/stats");
      if (!ticket.current()) return;
      const setMetric = (name, value) => { $(`[data-metric="${name}"]`).textContent = value; };
      setMetric("cpu", data.cpu_percent === null ? "Unavailable" : `${Number(data.cpu_percent).toFixed(1)}%`);
      setMetric("memory", data.memory ? `${Number(data.memory.percent).toFixed(1)}%` : "Unavailable");
      setMetric("disk", data.disk ? `${Number(data.disk.percent).toFixed(1)}%` : "Unavailable");
      setMetric("uptime", formatUptime(data.uptime_seconds));
      $("[data-detail='memory']").textContent = data.memory ? `${formatBytes(data.memory.used)} of ${formatBytes(data.memory.total)}` : "Host metric unavailable";
      $("[data-detail='disk']").textContent = data.disk ? `${formatBytes(data.disk.used)} of ${formatBytes(data.disk.total)}` : "Host metric unavailable";
      $("[data-detail='timezone']").textContent = data.timezone ? `${data.platform || "Host"} · ${data.timezone}` : "Mini PC local time";
      $("[data-meter='cpu']").style.width = `${Math.min(100, data.cpu_percent || 0)}%`;
      $("[data-meter='memory']").style.width = `${Math.min(100, data.memory?.percent || 0)}%`;
      $("[data-meter='disk']").style.width = `${Math.min(100, data.disk?.percent || 0)}%`;
    } catch (error) { if (ticket.current()) toast(error.message, "error"); }
  }

  async function loadOverview() {
    loadStats();
    const ticket = version("overview");
    const requests = [api("/reminders/all"), api("/jokes"), api("/wishlist/all")];
    if (state.guildId) requests.push(api(`/keywords/get?${query({ guild_id: state.guildId })}`)); else requests.push(Promise.resolve({}));
    const results = await Promise.allSettled(requests);
    if (!ticket.current()) return;
    const values = results.map((result) => result.status === "fulfilled" ? result.value : null);
    const keywordCount = values[3] ? Object.values(values[3]).reduce((sum, items) => sum + items.length, 0) : "—";
    const counts = [keywordCount, values[0]?.length ?? "—", values[1]?.length ?? "—", values[2]?.length ?? "—"];
    $$("#record-counts strong").forEach((node, index) => { node.textContent = counts[index]; });
    if (state.guildId) {
      const params = { guild_id: state.guildId, limit: "6" };
      if (state.userId) params.user_id = state.userId;
      try {
        const [ranking, feedback] = await Promise.all([api(`/keywords/top?${query(params)}`), api(`/llm/feedback/summary?${query({ guild_id: state.guildId })}`)]);
        if (!ticket.current()) return;
        renderRankings($("#overview-keywords"), ranking.keywords);
        renderFeedback($("#overview-feedback"), feedback.groups);
      } catch (error) { if (ticket.current()) toast(error.message, "error"); }
    } else {
      $("#overview-keywords").innerHTML = '<div class="empty">Enter a server ID above.</div>';
      $("#overview-feedback").innerHTML = '<div class="empty">Enter a server ID above.</div>';
    }
  }

  async function loadKeywords() {
    const target = $("#keywords-list");
    let guild;
    try { guild = requireGuild(); } catch (error) { target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; return; }
    const ticket = version("keywords");
    target.innerHTML = '<div class="empty">Loading responses…</div>';
    try {
      const params = { guild_id: guild, limit: "20" };
      if (state.userId) params.user_id = state.userId;
      const [responses, ranking] = await Promise.all([api(`/keywords/get?${query({ guild_id: guild })}`), api(`/keywords/top?${query(params)}`)]);
      if (!ticket.current()) return;
      const groups = Object.entries(responses).filter(([, items]) => items.length);
      target.innerHTML = groups.length ? `<div class="keyword-groups">${groups.map(([keyword, items]) => `<section class="keyword-group"><header class="keyword-group-header"><div><span class="field-label">Keyword</span><h3>${escapeHtml(keyword)}</h3><span class="cell-muted">${items.length} ${items.length === 1 ? "response" : "responses"}</span></div><button class="button danger ghost small" data-action="delete-keyword" data-keyword="${escapeHtml(keyword)}">Delete keyword</button></header><div class="keyword-response-list">${items.map((response, index) => `<div class="keyword-response-row"><div class="keyword-response-content"><span class="field-label">Response ${index + 1}</span><div class="keyword-response-text">${escapeHtml(response)}</div></div><button class="button danger ghost small" data-action="delete-keyword-response" data-keyword="${escapeHtml(keyword)}" data-response="${escapeHtml(response)}">Delete response</button></div>`).join("")}</div></section>`).join("")}</div>` : '<div class="empty">No keyword responses configured for this server.</div>';
      renderRankings($("#keyword-ranking"), ranking.keywords);
    } catch (error) { if (ticket.current()) target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }

  async function loadReminders() {
    const target = $("#reminders-list"); const ticket = version("reminders");
    target.innerHTML = '<div class="empty">Loading reminders…</div>';
    try {
      const rows = await api("/reminders/all"); if (!ticket.current()) return;
      target.innerHTML = rows.length ? `<table class="data-table"><thead><tr><th>When</th><th>Recipient</th><th>Message</th><th></th></tr></thead><tbody>${rows.map((row) => `<tr><td>${escapeHtml(formatDate(row.remind_at))}</td><td>User ${escapeHtml(row.user_id)}<span class="cell-muted">Channel ${escapeHtml(row.channel_id)}</span></td><td>${escapeHtml(row.message)}</td><td class="actions"><button class="button danger ghost small" data-action="delete-reminder" data-id="${row.id}">Delete</button></td></tr>`).join("")}</tbody></table>` : '<div class="empty">No reminders are waiting.</div>';
    } catch (error) { if (ticket.current()) target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }

  async function loadJokes() {
    const ticket = version("jokes");
    try {
      const [jokes, schedules] = await Promise.all([api("/jokes"), api("/jokes/guilds")]); if (!ticket.current()) return;
      $("#jokes-list").innerHTML = jokes.length ? `<table class="data-table"><thead><tr><th>Joke</th><th></th></tr></thead><tbody>${jokes.map((joke) => `<tr><td>${escapeHtml(joke.text)}</td><td class="actions"><button class="button secondary small" data-action="edit-joke" data-id="${joke.id}" data-text="${escapeHtml(joke.text)}">Edit</button><button class="button danger ghost small" data-action="delete-joke" data-id="${joke.id}">Delete</button></td></tr>`).join("")}</tbody></table>` : '<div class="empty">The joke pool is empty.</div>';
      $("#joke-schedules-list").innerHTML = schedules.length ? `<table class="data-table"><thead><tr><th>Server</th><th>Channel</th><th>Time</th><th>Last sent</th></tr></thead><tbody>${schedules.map((item) => `<tr><td>${escapeHtml(item.guild_id)}</td><td>${escapeHtml(item.channel_id)}</td><td>${escapeHtml(item.send_time)}</td><td>${escapeHtml(item.last_sent_date || "Never")}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No server has an active joke schedule.</div>';
      const own = schedules.find((item) => item.guild_id === state.guildId);
      if (own) { $("[name='channel_id']", $("#joke-schedule-form")).value = own.channel_id; $("[name='send_time']", $("#joke-schedule-form")).value = own.send_time; }
    } catch (error) { if (ticket.current()) toast(error.message, "error"); }
  }

  async function loadWishlist() {
    const target = $("#wishlist-list"); let user;
    try { user = requireUser(); } catch (error) { target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; return; }
    const ticket = version("wishlist"); target.innerHTML = '<div class="empty">Loading products…</div>';
    try {
      const all = await api("/wishlist/all"); if (!ticket.current()) return;
      state.wishlist = all.filter((item) => item.user_id === user);
      target.innerHTML = state.wishlist.length ? `<table class="data-table"><thead><tr><th>Product</th><th>Price</th><th>Status</th></tr></thead><tbody>${state.wishlist.map((item) => `<tr data-select="wishlist" data-id="${item.id}" class="${state.selectedWishlist === item.id ? "selected" : ""}"><td>${escapeHtml(item.title || "Untitled product")}<span class="cell-muted">${escapeHtml(hostname(item.url))}</span></td><td>${formatPrice(item.last_price, item.currency)}</td><td><span class="tag ${item.in_stock === true ? "good" : item.in_stock === false ? "bad" : ""}">${item.in_stock === true ? "in stock" : item.in_stock === false ? "out of stock" : "unknown"}</span></td></tr>`).join("")}</tbody></table>` : '<div class="empty">No products are tracked for this user.</div>';
      if (state.selectedWishlist && state.wishlist.some((item) => item.id === state.selectedWishlist)) loadWishlistDetail(state.selectedWishlist);
      else { state.selectedWishlist = null; $("#wishlist-detail").innerHTML = '<div class="empty">Select a product to inspect history and preferences.</div>'; }
    } catch (error) { if (ticket.current()) target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }

  function filterHistory(rows, days) {
    if (!days) return rows;
    const cutoff = Date.now() / 1000 - days * 86400;
    return rows.filter((row) => Number(row.timestamp ?? row.checked_at) >= cutoff);
  }

  function chartMarkup(rows, currency, type) {
    const filtered = filterHistory(rows, state.ranges[type]);
    if (!filtered.length) return '<div class="empty">No saved prices in this period.</div>';
    const points = filtered.map((row) => ({ x: Number(row.timestamp ?? row.checked_at), y: Number(row.price) })).filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    if (!points.length) return '<div class="empty">No numeric prices to chart.</div>';
    const width = 440, height = 190, left = 48, right = 12, top = 12, bottom = 31;
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minYRaw = Math.min(...ys), maxYRaw = Math.max(...ys);
    const padY = Math.max((maxYRaw - minYRaw) * .12, maxYRaw * .01, 1);
    const minY = minYRaw - padY, maxY = maxYRaw + padY;
    const px = (x) => left + (maxX === minX ? .5 : (x - minX) / (maxX - minX)) * (width - left - right);
    const py = (y) => top + (1 - (y - minY) / (maxY - minY)) * (height - top - bottom);
    const path = points.map((p, index) => `${index ? "L" : "M"}${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(" ");
    const grid = [0, .5, 1].map((ratio) => { const y = top + ratio * (height - top - bottom); const value = maxY - ratio * (maxY - minY); return `<line class="grid" x1="${left}" x2="${width-right}" y1="${y}" y2="${y}"/><text x="2" y="${y+3}">${escapeHtml(value.toFixed(0))}</text>`; }).join("");
    const range = state.ranges[type];
    return `<div class="chart-toolbar"><small>${points.length} observation${points.length === 1 ? "" : "s"}</small><div class="range-buttons">${[[7,"7d"],[30,"30d"],[90,"90d"],[0,"All"]].map(([value,label]) => `<button class="${range === value ? "active" : ""}" data-action="chart-range" data-chart="${type}" data-days="${value}">${label}</button>`).join("")}</div></div><div class="chart"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Saved price history">${grid}<path class="line" d="${path}"/>${points.map((p) => `<circle class="dot" cx="${px(p.x)}" cy="${py(p.y)}" r="3"/>`).join("")}<text x="${left}" y="${height-7}">${escapeHtml(new Date(minX*1000).toLocaleDateString())}</text><text x="${width-right}" y="${height-7}" text-anchor="end">${escapeHtml(new Date(maxX*1000).toLocaleDateString())}</text></svg></div><div class="chart-values">Low ${formatPrice(minYRaw, currency)} · High ${formatPrice(maxYRaw, currency)} · Latest ${formatPrice(points.at(-1).y, currency)}</div>`;
  }

  async function loadWishlistDetail(id) {
    const item = state.wishlist.find((entry) => entry.id === Number(id)); if (!item) return;
    state.selectedWishlist = item.id; const target = $("#wishlist-detail"); const ticket = version("wishlist-detail");
    target.innerHTML = '<div class="empty">Loading product history…</div>';
    try {
      const data = await api(`/wishlist/history?${query({ user_id: item.user_id, url: item.url })}`); if (!ticket.current()) return;
      target.innerHTML = `<div class="detail-heading"><h3>${escapeHtml(item.title || "Untitled product")}</h3><a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.url)}</a></div><div class="detail-stats"><div><small>Current</small><strong>${formatPrice(item.last_price, item.currency)}</strong></div><div><small>Target</small><strong>${formatPrice(item.target_price, item.target_currency)}</strong></div><div><small>Checked</small><strong>${escapeHtml(formatDate(item.last_checked_at))}</strong></div></div>${chartMarkup(data.history, item.currency, "wishlist")}<form id="wishlist-preferences-form" class="form-grid detail-form"><input type="hidden" name="url" value="${escapeHtml(item.url)}"><label>Target price<input name="target_price" type="number" min="0.01" step="0.01" value="${item.target_price ?? ""}"></label><label>Currency<select name="target_currency">${["EUR","RON","USD","GBP","DKK"].map((currency) => `<option ${item.target_currency === currency ? "selected" : ""}>${currency}</option>`).join("")}</select></label><label><span>Restock alerts only</span><select name="restock_only"><option value="false" ${!item.restock_only ? "selected" : ""}>No</option><option value="true" ${item.restock_only ? "selected" : ""}>Yes</option></select></label><div class="button-row"><button class="button primary">Save</button><button type="button" class="button secondary" data-action="refresh-wishlist-item" data-id="${item.id}">Refresh price</button><button type="button" class="button danger ghost" data-action="delete-wishlist-item" data-id="${item.id}">Remove</button></div></form>`;
      bindDynamicForms();
    } catch (error) { if (ticket.current()) target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }

  async function loadFlights() {
    const target = $("#flights-list"); let user;
    try { user = requireUser(); } catch (error) { target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; return; }
    const ticket = version("flights"); target.innerHTML = '<div class="empty">Loading flight trackers…</div>';
    try {
      const [credential, response] = await Promise.all([api(`/flights/credentials?${query({ user_id: user })}`), api(`/flights/trackers?${query({ user_id: user })}`)]); if (!ticket.current()) return;
      const badge = $("#credential-status"); badge.textContent = credential.configured ? "configured" : "not configured"; badge.className = `scope-pill ${credential.configured ? "global" : ""}`;
      state.flights = response.trackers;
      target.innerHTML = state.flights.length ? `<table class="data-table"><thead><tr><th>Route</th><th>Dates</th><th>Latest</th></tr></thead><tbody>${state.flights.map((item) => `<tr data-select="flight" data-id="${item.id}" class="${state.selectedFlight === item.id ? "selected" : ""}"><td>${escapeHtml(item.origin)} → ${escapeHtml(item.destination)}<span class="cell-muted">${item.adults} adult${item.adults === 1 ? "" : "s"}</span></td><td>${escapeHtml(item.start_date)}<span class="cell-muted">return ${escapeHtml(item.end_date)}</span></td><td>${formatPrice(item.last_price, item.currency)}${item.last_error ? `<span class="cell-muted">${escapeHtml(item.last_error)}</span>` : ""}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No flight trackers for this user.</div>';
      if (state.selectedFlight && state.flights.some((item) => item.id === state.selectedFlight)) loadFlightDetail(state.selectedFlight);
      else { state.selectedFlight = null; $("#flight-detail").innerHTML = '<div class="empty">Select a tracker to inspect its saved prices.</div>'; }
    } catch (error) { if (ticket.current()) target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }

  async function loadFlightDetail(id) {
    const item = state.flights.find((entry) => entry.id === Number(id)); if (!item) return;
    state.selectedFlight = item.id; const target = $("#flight-detail"); const ticket = version("flight-detail");
    target.innerHTML = '<div class="empty">Loading saved prices…</div>';
    try {
      const data = await api(`/flights/trackers/${item.id}/history?${query({ user_id: item.user_id })}`); if (!ticket.current()) return;
      target.innerHTML = `<div class="detail-heading"><h3>${escapeHtml(item.origin)} → ${escapeHtml(item.destination)}</h3><p>${escapeHtml(item.start_date)} to ${escapeHtml(item.end_date)} · ${item.adults} adult${item.adults === 1 ? "" : "s"}</p></div><div class="detail-stats"><div><small>Latest</small><strong>${formatPrice(item.last_price, item.currency)}</strong></div><div><small>Checked</small><strong>${escapeHtml(formatDate(item.last_checked_at))}</strong></div><div><small>Status</small><strong>${escapeHtml(item.last_error || "OK")}</strong></div></div>${chartMarkup(data.history, item.currency, "flight")}<div class="button-row detail-form"><button class="button danger ghost" data-action="delete-flight" data-id="${item.id}">Delete tracker</button></div>`;
    } catch (error) { if (ticket.current()) target.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }

  function selectedMemoryScope() {
    return $("#memory-scope").value === "dm" ? "0" : requireGuild();
  }

  async function loadMemory() {
    const ticket = version("memory");
    const channelsTarget = $("#memory-channels-list");
    const summaryTarget = $("#memory-user-summary");
    const entriesTarget = $("#memory-entries-list");
    const transcriptTarget = $("#memory-transcript-list");

    if (state.guildId) {
      try {
        const data = await api(`/memory/channels?${query({ guild_id: state.guildId })}`);
        if (!ticket.current()) return;
        channelsTarget.innerHTML = data.channels.length
          ? `<div class="button-row">${data.channels.map((item) => `<span class="tag good">${escapeHtml(item.channel_id)}</span>`).join("")}</div>`
          : '<div class="empty">No channels have memory enabled in this server.</div>';
      } catch (error) { if (ticket.current()) channelsTarget.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
    } else channelsTarget.innerHTML = '<div class="empty">Enter a server ID to manage channels.</div>';

    if (!state.userId) {
      summaryTarget.innerHTML = '<div class="empty">Enter a user ID to inspect memory.</div>';
      entriesTarget.innerHTML = '<div class="empty">No user selected.</div>';
      transcriptTarget.innerHTML = '<div class="empty">No user selected.</div>';
      return;
    }
    let scope;
    try { scope = selectedMemoryScope(); } catch (error) {
      summaryTarget.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      entriesTarget.innerHTML = '<div class="empty">No scope selected.</div>';
      transcriptTarget.innerHTML = '<div class="empty">No scope selected.</div>';
      return;
    }
    try {
      const data = await api(`/memory/users/${requireUser()}?${query({ scope_id: scope })}`);
      if (!ticket.current()) return;
      const preference = data.preference === true ? '<span class="tag good">opted in</span>' : data.preference === false ? '<span class="tag bad">opted out</span>' : '<span class="tag">default</span>';
      summaryTarget.innerHTML = `<div class="detail-stats"><div><small>Preference</small><strong>${preference}</strong></div><div><small>Synthesized entries</small><strong>${data.entries.length}</strong></div></div>${data.legacy_profile ? `<div class="memory-message"><small>Legacy profile</small><p>${escapeHtml(data.legacy_profile)}</p></div>` : ""}`;
      entriesTarget.innerHTML = data.entries.length ? `<table class="data-table"><thead><tr><th>Type</th><th>Memory</th><th>Updated</th></tr></thead><tbody>${data.entries.map((entry) => `<tr><td><span class="tag">${escapeHtml(entry.kind)}</span></td><td class="memory-content">${escapeHtml(entry.content)}</td><td>${escapeHtml(formatDate(entry.updated_at))}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No daily memory synthesis has been saved for this user.</div>';
      transcriptTarget.innerHTML = '<div class="empty">Raw conversation is not stored as durable memory.</div>';
    } catch (error) { if (ticket.current()) { summaryTarget.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; entriesTarget.innerHTML = '<div class="empty">Could not load entries.</div>'; transcriptTarget.innerHTML = '<div class="empty">Could not load conversation.</div>'; } }
  }

  async function loadSettings() {
    const ticket = version("settings");
    try {
      const model = await api("/llm/mention-model"); if (!ticket.current()) return;
      $("#model-select").innerHTML = model.allowed_models.map((name) => `<option ${name === model.model ? "selected" : ""}>${escapeHtml(name)}</option>`).join("");
      if (state.guildId) {
        const [inactivity, feedback] = await Promise.all([api(`/inactivity/guilds/${state.guildId}`), api(`/llm/feedback/summary?${query({ guild_id: state.guildId })}`)]); if (!ticket.current()) return;
        $("#inactivity-state").innerHTML = `<span class="tag ${inactivity.enabled ? "good" : "bad"}">${inactivity.enabled ? "Enabled" : "Disabled"}</span>`;
        renderFeedback($("#settings-feedback"), feedback.groups);
      } else {
        $("#inactivity-state").innerHTML = '<div class="empty">Enter a server ID.</div>';
        $("#settings-feedback").innerHTML = '<div class="empty">Enter a server ID to load feedback.</div>';
      }
    } catch (error) { if (ticket.current()) toast(error.message, "error"); }
  }

  function renderAnalyticsCommands() {
    const target = $("#analytics-commands");
    const rows = state.analytics?.commands?.active || [];
    const ascending = $("#analytics-command-sort").value === "asc";
    const sorted = [...rows].sort((a, b) => ascending
      ? a.count - b.count || a.name.localeCompare(b.name)
      : b.count - a.count || a.name.localeCompare(b.name));
    target.innerHTML = sorted.length ? `<table class="data-table"><thead><tr><th>Command</th><th>Uses</th><th>Latest in period</th></tr></thead><tbody>${sorted.map((row) => `<tr><td><code>/${escapeHtml(row.name)}</code></td><td>${row.count}</td><td>${escapeHtml(formatDate(row.latest_at))}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No active commands are registered yet.</div>';
  }

  function renderAnalyticsList(target, rows, emptyText, nameKey = "name") {
    target.innerHTML = rows?.length ? `<table class="data-table"><thead><tr><th>Activity</th><th>Category</th><th>Count</th><th>Latest</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${nameKey === "name" ? `<code>/${escapeHtml(row[nameKey])}</code>` : escapeHtml(row[nameKey])}</td><td>${escapeHtml(row.category || (row.active === false ? "inactive command" : "command"))}</td><td>${row.count}</td><td>${escapeHtml(formatDate(row.latest_at))}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">${escapeHtml(emptyText)}</div>`;
  }

  function renderAnalyticsChart(rows) {
    const target = $("#analytics-chart");
    if (!rows?.length || !rows.some((row) => row.total > 0)) {
      target.innerHTML = '<div class="empty">No activity has been recorded in this period.</div>';
      return;
    }
    const width = 900; const height = 190; const pad = 24;
    const max = Math.max(...rows.map((row) => row.total), 1);
    const points = rows.map((row, index) => {
      const x = rows.length === 1 ? width / 2 : pad + index * (width - pad * 2) / (rows.length - 1);
      const y = height - pad - row.total / max * (height - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily bot activity"><line class="grid" x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}"></line><polyline class="line" points="${points}"></polyline><text x="${pad}" y="${height - 5}">${escapeHtml(rows[0].date)}</text><text text-anchor="end" x="${width - pad}" y="${height - 5}">${escapeHtml(rows[rows.length - 1].date)}</text><text x="${pad}" y="14">max ${max}</text></svg>`;
  }

  async function loadAnalytics() {
    const ticket = version("analytics");
    const params = { period: $("#analytics-period").value };
    if (state.guildId) params.guild_id = state.guildId;
    try {
      const data = await api(`/analytics/summary?${query(params)}`);
      if (!ticket.current()) return;
      state.analytics = data;
      $("#analytics-command-total").textContent = data.category_totals.command || 0;
      $("#analytics-mention-total").textContent = data.category_totals.mention || 0;
      $("#analytics-automatic-total").textContent = data.category_totals.automatic || 0;
      $("#analytics-scheduled-total").textContent = data.category_totals.scheduled || 0;
      $("#analytics-failure-total").textContent = data.category_totals.failure || 0;
      $("#analytics-tracking").textContent = `Tracking since ${data.tracking_started_date} · showing ${data.period.start} to ${data.period.end} UTC`;
      $("#analytics-scope").textContent = state.guildId ? `server ${state.guildId}` : `all · server ${data.scope_totals.guild} · DM ${data.scope_totals.dm} · global ${data.scope_totals.global}`;
      renderAnalyticsCommands();
      renderAnalyticsList($("#analytics-unused"), data.commands.unused, "Every active command was used in this period.");
      renderAnalyticsList($("#analytics-features"), data.features, "No non-command activity in this period.", "activity");
      renderAnalyticsList($("#analytics-failures"), data.failures, "No failures in this period.", "activity");
      renderAnalyticsList($("#analytics-inactive"), data.commands.inactive, "No inactive commands have historical data.");
      renderAnalyticsChart(data.daily);
    } catch (error) {
      if (!ticket.current()) return;
      state.analytics = null;
      $("#analytics-chart").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
      $("#analytics-commands").innerHTML = '<div class="empty">Could not load command analytics.</div>';
      $("#analytics-unused").innerHTML = '<div class="empty">Could not load unused commands.</div>';
      $("#analytics-features").innerHTML = '<div class="empty">Could not load feature analytics.</div>';
      $("#analytics-failures").innerHTML = '<div class="empty">Could not load failure analytics.</div>';
      $("#analytics-inactive").innerHTML = '<div class="empty">Could not load inactive commands.</div>';
    }
  }

  const loaders = { overview: loadOverview, keywords: loadKeywords, reminders: loadReminders, jokes: loadJokes, wishlist: loadWishlist, flights: loadFlights, memory: loadMemory, analytics: loadAnalytics, settings: loadSettings };
  function navigate(page) {
    if (!loaders[page]) return;
    state.page = page;
    $$(".page").forEach((node) => node.classList.toggle("active", node.id === `page-${page}`));
    $$(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.page === page));
    $("#page-title").textContent = $(`#page-${page}`).dataset.title;
    $("#global-user-scope").hidden = page === "analytics";
    document.body.classList.remove("menu-open");
    clearInterval(state.statsTimer); state.statsTimer = null;
    loaders[page]();
    if (page === "overview") state.statsTimer = setInterval(loadStats, 15000);
  }

  function bindDynamicForms() {
    const form = $("#wishlist-preferences-form");
    if (form && !form.dataset.bound) {
      form.dataset.bound = "true";
      form.addEventListener("submit", (event) => { event.preventDefault(); submit(form, async (data) => {
        const payload = { user_id: requireUser(), url: data.get("url"), restock_only: data.get("restock_only") === "true" };
        if (data.get("target_price")) { payload.target_price = Number(data.get("target_price")); payload.target_currency = data.get("target_currency"); }
        else payload.clear_target = true;
        await api("/wishlist/preferences", { method: "PUT", body: JSON.stringify(payload) }); await loadWishlist();
      }, "Wishlist preferences saved."); });
    }
  }

  function bindForms() {
    $("#keyword-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api("/keywords/add", { method: "POST", body: JSON.stringify({ guild_id: requireGuild(), keyword: data.get("keyword"), response: data.get("response") }) }); event.currentTarget.reset(); await loadKeywords(); }, "Keyword response added."); });
    $("#reminder-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api("/reminders/add", { method: "POST", body: JSON.stringify({ user_id: requireUser(), channel_id: idValue(data.get("channel_id"), "Channel ID"), remind_at: unixSeconds(data.get("when")), message: data.get("message") }) }); event.currentTarget.reset(); await loadReminders(); }, "Reminder scheduled."); });
    $("#joke-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api("/jokes", { method: "POST", body: JSON.stringify({ text: data.get("text") }) }); event.currentTarget.reset(); await loadJokes(); }, "Joke added."); });
    $("#joke-schedule-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api(`/jokes/guilds/${requireGuild()}`, { method: "PUT", body: JSON.stringify({ channel_id: idValue(data.get("channel_id"), "Channel ID"), send_time: data.get("send_time") }) }); await loadJokes(); }, "Joke schedule saved."); });
    $("#wishlist-add-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api("/wishlist/add", { method: "POST", body: JSON.stringify({ user_id: requireUser(), url: data.get("url") }) }); event.currentTarget.reset(); await loadWishlist(); }, "Product added to the wishlist."); });
    $("#credential-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api("/flights/credentials", { method: "POST", body: JSON.stringify({ user_id: requireUser(), api_key: data.get("api_key") }) }); event.currentTarget.reset(); await loadFlights(); }, "Credentials validated and saved."); });
    $("#flight-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api("/flights/trackers", { method: "POST", body: JSON.stringify({ user_id: requireUser(), origin: data.get("origin"), destination: data.get("destination"), start_date: data.get("start_date"), end_date: data.get("end_date"), adults: Number(data.get("adults")), currency: data.get("currency") }) }); event.currentTarget.reset(); await loadFlights(); }, "Flight tracker added."); });
    $("#model-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api("/llm/mention-model", { method: "PUT", body: JSON.stringify({ model: data.get("model") }) }); await loadSettings(); }, "Mention model updated."); });
    $("#setting-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async (data) => { await api(`/settings/${encodeURIComponent(data.get("key"))}`, { method: "PUT", body: JSON.stringify({ value: data.get("value") }) }); }, "Setting saved."); });
  }

  async function handleAction(button) {
    const action = button.dataset.action;
    try {
      if (action === "refresh-overview") return loadOverview();
      if (action === "logout") { state.token = ""; sessionStorage.removeItem("bot-dashboard-token"); showLogin(); return; }
      if (action === "load-keywords") return loadKeywords();
      if (action === "load-reminders") return loadReminders();
      if (action === "load-wishlist") return loadWishlist();
      if (action === "load-flights") return loadFlights();
      if (action === "load-memory") return loadMemory();
      if (action === "load-settings") return loadSettings();
      if (action === "load-analytics") return loadAnalytics();
      if (action === "delete-keyword-response" || action === "delete-keyword") {
        if (!confirm(action.endsWith("response") ? "Delete this response?" : "Delete every response for this keyword?")) return;
        const payload = { guild_id: requireGuild(), keyword: button.dataset.keyword };
        if (action.endsWith("response")) payload.response = button.dataset.response;
        await api("/keywords/delete", { method: "DELETE", body: JSON.stringify(payload) }); toast(action.endsWith("response") ? "Keyword response removed." : "Keyword deleted."); return loadKeywords();
      }
      if (action === "delete-reminder") { if (!confirm("Delete this reminder?")) return; await api(`/reminders/delete/${button.dataset.id}`, { method: "DELETE" }); toast("Reminder deleted."); return loadReminders(); }
      if (action === "delete-joke") { if (!confirm("Delete this joke from the global pool?")) return; await api(`/jokes/${button.dataset.id}`, { method: "DELETE" }); toast("Joke deleted."); return loadJokes(); }
      if (action === "edit-joke") { const text = prompt("Edit joke text", button.dataset.text); if (text === null) return; if (!text.trim()) throw new Error("Joke text cannot be empty."); await api(`/jokes/${button.dataset.id}`, { method: "PUT", body: JSON.stringify({ text: text.trim() }) }); toast("Joke updated."); return loadJokes(); }
      if (action === "reset-jokes") { if (!confirm("Reset sent history for every server? The joke pool stays intact.")) return; await api("/jokes/reset", { method: "POST", body: "{}" }); return toast("Sent history reset."); }
      if (action === "delete-joke-schedule") { if (!confirm("Remove the selected server's joke schedule?")) return; await api(`/jokes/guilds/${requireGuild()}`, { method: "DELETE" }); toast("Schedule removed."); return loadJokes(); }
      if (action === "refresh-wishlist-item") { const item = state.wishlist.find((entry) => entry.id === Number(button.dataset.id)); button.disabled = true; await api("/wishlist/refresh", { method: "POST", body: JSON.stringify({ user_id: requireUser(), url: item.url }) }); toast("Product refreshed."); return loadWishlist(); }
      if (action === "delete-wishlist-item") { const item = state.wishlist.find((entry) => entry.id === Number(button.dataset.id)); if (!confirm("Stop tracking this product and delete its history?")) return; await api("/wishlist/remove", { method: "DELETE", body: JSON.stringify({ user_id: requireUser(), url: item.url }) }); state.selectedWishlist = null; toast("Product removed."); return loadWishlist(); }
      if (action === "delete-credentials") { if (!confirm("Delete this user's saved SerpApi credentials?")) return; await api("/flights/credentials", { method: "DELETE", body: JSON.stringify({ user_id: requireUser() }) }); toast("Credentials removed."); return loadFlights(); }
      if (action === "delete-flight") { if (!confirm("Delete this flight tracker and its price history?")) return; await api(`/flights/trackers/${button.dataset.id}`, { method: "DELETE", body: JSON.stringify({ user_id: requireUser() }) }); state.selectedFlight = null; toast("Flight tracker deleted."); return loadFlights(); }
      if (action === "check-memory-channel") { const channel = idValue(new FormData($("#memory-channel-form")).get("channel_id"), "Channel ID"); const data = await api(`/memory/channels/${requireGuild()}/${channel}`); $("#memory-channel-state").innerHTML = `<span class="tag ${data.enabled ? "good" : "bad"}">${data.enabled ? "Enabled" : "Disabled"}</span>`; return; }
      if (action === "set-memory-channel") { const channel = idValue(new FormData($("#memory-channel-form")).get("channel_id"), "Channel ID"); const enabled = button.dataset.enabled === "true"; await api(`/memory/channels/${requireGuild()}/${channel}`, { method: "PUT", body: JSON.stringify({ enabled }) }); $("#memory-channel-state").innerHTML = `<span class="tag ${enabled ? "good" : "bad"}">${enabled ? "Enabled" : "Disabled"}</span>`; toast(`Channel memory ${enabled ? "enabled" : "disabled"}.`); return loadMemory(); }
      if (action === "set-memory-preference") { const enabled = button.dataset.enabled === "true"; if (!enabled && !confirm("Opt this user out and permanently erase their saved memory in this scope?")) return; await api(`/memory/users/${requireUser()}/preference`, { method: "PUT", body: JSON.stringify({ scope_id: selectedMemoryScope(), enabled }) }); toast(enabled ? "User opted in." : "User opted out and memory erased."); return loadMemory(); }
      if (action === "forget-memory-user") { if (!confirm("Erase this user's saved memory and pending observations in the selected scope? Their opt-in preference will stay unchanged.")) return; await api(`/memory/users/${requireUser()}`, { method: "DELETE", body: JSON.stringify({ scope_id: selectedMemoryScope() }) }); toast("User memory erased."); return loadMemory(); }
      if (action === "purge-memory-guild") { const confirmation = prompt("Type PURGE to erase every user's saved memory in the selected server."); if (confirmation === null) return; await api(`/memory/guilds/${requireGuild()}`, { method: "DELETE", body: JSON.stringify({ confirmation }) }); toast("Server memory purged."); return loadMemory(); }
      if (action === "set-inactivity") { await api(`/inactivity/guilds/${requireGuild()}`, { method: "PUT", body: JSON.stringify({ enabled: button.dataset.enabled === "true" }) }); toast("Inactivity setting updated."); return loadSettings(); }
      if (action === "read-setting") { const form = $("#setting-form"); const key = new FormData(form).get("key"); if (!key) throw new Error("Enter a setting key."); const data = await api(`/settings/${encodeURIComponent(key)}`); $("[name='value']", form).value = data.value; return toast("Setting loaded."); }
      if (action === "chart-range") { state.ranges[button.dataset.chart] = Number(button.dataset.days); return button.dataset.chart === "wishlist" ? loadWishlistDetail(state.selectedWishlist) : loadFlightDetail(state.selectedFlight); }
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
  }

  async function init() {
    $("#global-guild-id").value = state.guildId; $("#global-user-id").value = state.userId;
    $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.page)));
    $("#menu-button").addEventListener("click", () => document.body.classList.toggle("menu-open"));
    $("#scrim").addEventListener("click", () => document.body.classList.remove("menu-open"));
    let scopeTimer;
    const updateScope = () => { clearTimeout(scopeTimer); scopeTimer = setTimeout(() => { state.guildId = $("#global-guild-id").value.trim(); state.userId = $("#global-user-id").value.trim(); localStorage.setItem("bot-dashboard-guild", state.guildId); localStorage.setItem("bot-dashboard-user", state.userId); state.selectedWishlist = null; state.selectedFlight = null; loaders[state.page](); }, 350); };
    $("#global-guild-id").addEventListener("input", updateScope); $("#global-user-id").addEventListener("input", updateScope);
    $("#memory-scope").addEventListener("change", loadMemory);
    $("#analytics-period").addEventListener("change", loadAnalytics);
    $("#analytics-command-sort").addEventListener("change", renderAnalyticsCommands);
    $("#login-form").addEventListener("submit", (event) => { event.preventDefault(); const form = event.currentTarget; submit(form, async (data) => login(data.get("token"))); });
    document.addEventListener("click", (event) => { const action = event.target.closest("[data-action]"); if (action) { event.preventDefault(); handleAction(action); return; } const row = event.target.closest("[data-select]"); if (row?.dataset.select === "wishlist") loadWishlistDetail(row.dataset.id); if (row?.dataset.select === "flight") loadFlightDetail(row.dataset.id); });
    bindForms();
    if (state.token) {
      try { await login(state.token); } catch (_error) { showLogin("Your saved session is no longer valid. Sign in again."); }
    } else showLogin();
  }

  window.DashboardTest = { idValue, unixSeconds, version };
  document.addEventListener("DOMContentLoaded", init);
})();
