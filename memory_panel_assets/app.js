const state = {
  token: new URLSearchParams(window.location.search).get("token") || "",
  selectedUserId: "",
  overview: null,
  detail: null,
  directoryUsers: [],
};

const PREFERENCE_OPTIONS = [
  { value: "like", label: "喜欢" },
  { value: "dislike", label: "不喜欢" },
  { value: "preferred_name", label: "称呼偏好" },
];

const BOOKKEEPING_TYPE_OPTIONS = [
  { value: "expense", label: "支出" },
  { value: "income", label: "收入" },
];

const SCENE_OPTIONS = [
  { value: "global", label: "全局" },
  { value: "group", label: "群聊" },
  { value: "private", label: "私聊" },
];

const SCENE_EDIT_OPTIONS = SCENE_OPTIONS.filter((item) => item.value !== "global");

const RELATION_OPTIONS = [
  { value: "朋友", label: "朋友" },
  { value: "同学", label: "同学" },
  { value: "同事", label: "同事" },
  { value: "家人", label: "家人" },
  { value: "亲戚", label: "亲戚" },
  { value: "对象", label: "对象" },
  { value: "室友", label: "室友" },
  { value: "搭子", label: "搭子" },
  { value: "老师", label: "老师" },
  { value: "同伴", label: "同伴" },
];

const HABIT_MODULES = [
  { value: "weather", label: "天气" },
  { value: "stock", label: "股票" },
  { value: "fund", label: "基金" },
  { value: "email", label: "邮件" },
  { value: "reminder", label: "提醒" },
  { value: "lol", label: "LOL" },
  { value: "chat", label: "聊天" },
];

const HABIT_KEYS = {
  weather: [{ value: "city", label: "常查城市" }],
  stock: [
    { value: "stock_code", label: "常看股票" },
    { value: "reminder_time", label: "提醒时间" },
  ],
  fund: [
    { value: "fund_code", label: "常看基金" },
    { value: "default_fund_code", label: "默认基金" },
  ],
  email: [{ value: "recipient", label: "常用收件人" }],
  reminder: [{ value: "reminder_time", label: "常设提醒时间" }],
  lol: [
    { value: "summoner_name", label: "默认昵称" },
    { value: "area_name", label: "常用大区" },
  ],
  chat: [{ value: "routine", label: "常聊习惯" }],
};

const SCENE_LABELS = Object.fromEntries(SCENE_OPTIONS.map((item) => [item.value, item.label]));

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    "X-Memory-Token": state.token,
    ...(options.headers || {}),
  };
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderEmpty(container) {
  container.innerHTML = $("#empty-list-template").innerHTML;
}

function statCard(label, value) {
  return `<div class="stat-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function insightCard(label, value) {
  return `<div class="insight-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function optionHtml(option, selectedValue = "") {
  return `<option value="${escapeHtml(option.value)}" ${String(option.value) === String(selectedValue) ? "selected" : ""}>${escapeHtml(option.label)}</option>`;
}

function renderSelect(select, options, selectedValue = "", placeholder = "") {
  const placeholderOption = placeholder ? [{ value: "", label: placeholder }] : [];
  select.innerHTML = [...placeholderOption, ...options].map((item) => optionHtml(item, selectedValue)).join("");
}

function currentSourceText(formSelector) {
  return `${formSelector} [name='source_text']`;
}

function resetForm(formEl) {
  if (!(formEl instanceof HTMLFormElement)) return;
  formEl.reset();
}

function userDisplayName(user) {
  return user.memory_name || user.platform_name || user.qq_id || "未命名用户";
}

function formatMoney(value) {
  const amount = Number(value || 0);
  return `¥${amount.toFixed(2)}`;
}

function bookkeepingTypeLabel(value) {
  return value === "income" ? "收入" : "支出";
}

function directoryUserOptions() {
  return (state.directoryUsers || [])
    .filter((user) => String(user.qq_id) !== String(state.selectedUserId))
    .map((user) => ({
      value: user.qq_id,
      label: `${userDisplayName(user)}（QQ ${user.qq_id}）`,
    }));
}

function habitKeyOptions(moduleName) {
  return HABIT_KEYS[String(moduleName || "").trim()] || [];
}

function updateHero(detail) {
  const user = detail.user;
  const title = user.memory_name || user.platform_name || user.qq_id || "未命名用户";
  $("#hero-title").textContent = title;
  $("#hero-subtitle").textContent = `QQ ${user.qq_id} · ${user.note || "暂无备注"}`;
  $("#hero-last-seen").textContent = `最近出现: ${user.last_seen_at || "-"}`;
  $("#hero-updated").textContent = `最近更新: ${user.updated_at || "-"}`;
  $("#profile-form [name='platform_name']").value = user.platform_name || "";
  $("#profile-form [name='note']").value = user.note || "";
}

function renderStats(stats = {}) {
  $("#stats-grid").innerHTML = [
    statCard("用户", stats.users || 0),
    statCard("手动认人", stats.manual_users || 0),
    statCard("偏好", stats.preferences || 0),
    statCard("关系", stats.relations || 0),
    statCard("习惯", stats.habits || 0),
    statCard("事件", stats.events || 0),
    statCard("账本", stats.bookkeeping || 0),
    statCard("提醒", stats.reminders || 0),
  ].join("");
}

function renderUsers(users = []) {
  const container = $("#user-list");
  if (!users.length) {
    renderEmpty(container);
    return;
  }
  container.innerHTML = users.map((user) => `
    <div class="user-item ${state.selectedUserId === user.qq_id ? "active" : ""}" data-user-id="${escapeHtml(user.qq_id)}">
      <h3>${escapeHtml(userDisplayName(user))}</h3>
      <p>${escapeHtml(user.note || user.platform_name || "暂无备注")}</p>
      <small>QQ ${escapeHtml(user.qq_id)}</small>
      <div class="user-counts">
        <span class="count-badge">偏好 ${user.counts.preferences}</span>
        <span class="count-badge">关系 ${user.counts.relations}</span>
        <span class="count-badge">习惯 ${user.counts.habits}</span>
        <span class="count-badge">事件 ${user.counts.events}</span>
        <span class="count-badge">账本 ${user.counts.bookkeeping || 0}</span>
        <span class="count-badge">提醒 ${user.counts.reminders || 0}</span>
      </div>
    </div>
  `).join("");
  $$(".user-item").forEach((item) => {
    item.addEventListener("click", () => loadUser(item.dataset.userId));
  });
}

function aliasChip(alias) {
  return `
    <span class="chip">
      <span>${escapeHtml(alias)}</span>
      <button type="button" data-alias-delete="${escapeHtml(alias)}">删除</button>
    </span>
  `;
}

function buildFieldHtml(row, field) {
  const value = row[field.key] ?? "";
  if (field.type === "select") {
    const options = typeof field.options === "function" ? field.options(row) : (field.options || []);
    return `
      <select data-field="${escapeHtml(field.key)}">
        ${options.map((item) => optionHtml(item, value)).join("")}
      </select>
    `;
  }
  const attrs = field.extra || "";
  return `<input data-field="${escapeHtml(field.key)}" value="${escapeHtml(value)}" ${attrs}>`;
}

function renderEditableList(container, rows, config) {
  if (!rows.length) {
    renderEmpty(container);
    return;
  }
  container.innerHTML = rows.map((row) => `
    <div class="editable-item" data-id="${row.id}">
      <div class="editable-item-grid">
        ${config.fields.map((field) => buildFieldHtml(row, field)).join("")}
      </div>
      <div class="actions">
        <button type="button" class="mini-btn" data-save-id="${row.id}">保存</button>
        <button type="button" class="danger-btn" data-delete-id="${row.id}">删除</button>
      </div>
    </div>
  `).join("");
  container.querySelectorAll("[data-save-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      const card = button.closest(".editable-item");
      const payload = {};
      card.querySelectorAll("[data-field]").forEach((input) => {
        payload[input.dataset.field] = input.value;
      });
      if (typeof config.preparePayload === "function") {
        config.preparePayload(payload, card);
      }
      await config.onSave(button.dataset.saveId, payload);
    });
  });
  container.querySelectorAll("[data-delete-id]").forEach((button) => {
    button.addEventListener("click", async () => config.onDelete(button.dataset.deleteId));
  });
}

function renderScopedAliases(user) {
  renderEditableList($("#scoped-alias-list"), user.scoped_aliases || [], {
    fields: [
      { key: "scene_type", type: "select", options: SCENE_EDIT_OPTIONS },
      { key: "scene_value", extra: 'placeholder="群聊时填写群号"' },
      { key: "alias", extra: 'placeholder="场景称呼"' },
    ],
    preparePayload: (payload) => {
      if (payload.scene_type !== "group") {
        payload.scene_value = "";
      }
    },
    onSave: async (id, payload) => {
      if (payload.scene_type === "group" && !String(payload.scene_value || "").trim()) {
        throw new Error("群聊场景别名必须填写群号");
      }
      await api(`/api/scene-aliases/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await loadUser(state.selectedUserId);
      await refreshDirectoryUsers();
      await loadOverview($("#user-search").value.trim());
    },
    onDelete: async (id) => {
      await api(`/api/scene-aliases/${id}`, { method: "DELETE" });
      await loadUser(state.selectedUserId);
      await refreshDirectoryUsers();
      await loadOverview($("#user-search").value.trim());
    },
  });
}

function renderAliases(user) {
  const container = $("#alias-list");
  const aliases = user.memory_aliases || [];
  if (!aliases.length) {
    renderEmpty(container);
  } else {
    container.innerHTML = aliases.map(aliasChip).join("");
    container.querySelectorAll("[data-alias-delete]").forEach((button) => {
      button.addEventListener("click", async () => {
        await api(`/api/users/${state.selectedUserId}/aliases`, {
          method: "DELETE",
          body: JSON.stringify({
            alias: button.dataset.aliasDelete,
            scene_type: "global",
            scene_value: "",
          }),
        });
        await loadUser(state.selectedUserId);
        await refreshDirectoryUsers();
        await loadOverview($("#user-search").value.trim());
      });
    });
  }
  renderScopedAliases(user);
}

function renderGraph(graph) {
  const container = $("#graph-canvas");
  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  if (nodes.length <= 1) {
    renderEmpty(container);
    return;
  }
  const width = container.clientWidth || 520;
  const height = 320;
  const cx = width / 2;
  const cy = height / 2;
  const outer = Math.min(width, height) / 2 - 46;
  const people = nodes.slice(1);
  const positioned = people.map((node, index) => {
    const angle = (Math.PI * 2 * index) / people.length - Math.PI / 2;
    return {
      ...node,
      x: cx + Math.cos(angle) * outer,
      y: cy + Math.sin(angle) * outer,
    };
  });
  const byId = Object.fromEntries(positioned.map((node) => [node.id, node]));
  container.innerHTML = `
    <svg class="graph-svg" viewBox="0 0 ${width} ${height}">
      ${edges.map((edge) => {
        const target = byId[edge.target];
        if (!target) return "";
        const mx = (cx + target.x) / 2;
        const my = (cy + target.y) / 2 - 8;
        return `
          <line class="graph-line" x1="${cx}" y1="${cy}" x2="${target.x}" y2="${target.y}"></line>
          <text class="graph-edge-label" x="${mx}" y="${my}" text-anchor="middle">${escapeHtml(edge.label)}</text>
        `;
      }).join("")}
      <circle class="graph-node-self" cx="${cx}" cy="${cy}" r="26"></circle>
      <text class="graph-label" x="${cx}" y="${cy + 46}" text-anchor="middle">${escapeHtml(nodes[0].label)}</text>
      ${positioned.map((node) => `
        <circle class="graph-node-person" cx="${node.x}" cy="${node.y}" r="18"></circle>
        <text class="graph-label" x="${node.x}" y="${node.y + 36}" text-anchor="middle">${escapeHtml(node.label)}</text>
      `).join("")}
    </svg>
  `;
}

function renderTimeline(events = []) {
  const container = $("#events-list");
  if (!events.length) {
    renderEmpty(container);
    return;
  }
  container.innerHTML = events.map((item) => `
    <div class="timeline-item" data-id="${item.id}">
      <div class="timeline-meta">${escapeHtml(item.event_date_label || "未标注时间")} · 置信度 ${Number(item.confidence || 0).toFixed(2)}</div>
      <div class="editable-item-grid">
        <input data-field="event_date_label" value="${escapeHtml(item.event_date_label || "")}">
        <input data-field="summary" value="${escapeHtml(item.summary || "")}">
        <input data-field="confidence" type="number" step="0.01" min="0" max="1" value="${escapeHtml(item.confidence || 0.82)}">
        <input data-field="source_text" value="${escapeHtml(item.source_text || "")}">
      </div>
      <div class="actions">
        <button type="button" class="mini-btn" data-save-id="${item.id}">保存</button>
        <button type="button" class="danger-btn" data-delete-id="${item.id}">删除</button>
      </div>
    </div>
  `).join("");
  container.querySelectorAll("[data-save-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      const card = button.closest(".timeline-item");
      const payload = {};
      card.querySelectorAll("[data-field]").forEach((input) => {
        payload[input.dataset.field] = input.value;
      });
      await api(`/api/events/${button.dataset.saveId}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await loadUser(state.selectedUserId);
    });
  });
  container.querySelectorAll("[data-delete-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/events/${button.dataset.deleteId}`, { method: "DELETE" });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    });
  });
}

function renderBookkeeping(bookkeeping = {}) {
  const summary = bookkeeping.summary || {};
  $("#bookkeeping-summary").innerHTML = [
    insightCard("记录数", summary.total_records || 0),
    insightCard("总收入", formatMoney(summary.total_income || 0)),
    insightCard("总支出", formatMoney(summary.total_expense || 0)),
    insightCard("当前结余", formatMoney(summary.balance || 0)),
  ].join("");

  renderEditableList($("#bookkeeping-list"), bookkeeping.records || [], {
    fields: [
      { key: "type", type: "select", options: BOOKKEEPING_TYPE_OPTIONS },
      { key: "category", extra: 'placeholder="分类"' },
      { key: "amount", extra: 'type="number" step="0.01" min="0"' },
      { key: "created_at", extra: 'placeholder="YYYY-MM-DD HH:MM:SS"' },
      { key: "description", extra: 'placeholder="备注"' },
    ],
    preparePayload: (payload) => {
      payload.record_type = payload.type;
      delete payload.type;
    },
    onSave: async (id, payload) => {
      await api(`/api/users/${encodeURIComponent(state.selectedUserId)}/bookkeeping/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    },
    onDelete: async (id) => {
      await api(`/api/users/${encodeURIComponent(state.selectedUserId)}/bookkeeping/${id}`, {
        method: "DELETE",
      });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    },
  });
}

function renderReminderHistory(history = []) {
  const container = $("#reminder-history-list");
  if (!history.length) {
    renderEmpty(container);
    return;
  }
  container.innerHTML = history.map((item) => `
    <div class="timeline-item" data-id="${escapeHtml(item.id)}">
      <div class="timeline-meta">
        ${escapeHtml(item.status || "unknown")} · 触发 ${escapeHtml(item.run_at || "-")} · 会话 ${escapeHtml(item.session_id || "-")}
      </div>
      <div class="timeline-body">${escapeHtml(item.text || "")}</div>
      <div class="timeline-meta">归档 ${escapeHtml(item.archived_at || item.finished_at || item.created_at || "-")}</div>
      <div class="actions">
        <button type="button" class="danger-btn" data-delete-history-id="${escapeHtml(item.id)}">删除</button>
      </div>
    </div>
  `).join("");
  container.querySelectorAll("[data-delete-history-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/users/${encodeURIComponent(state.selectedUserId)}/simple-reminders/${button.dataset.deleteHistoryId}`, {
        method: "DELETE",
      });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    });
  });
}

function renderReminders(reminders = {}) {
  const pending = reminders.pending || [];
  const history = reminders.history || [];
  $("#reminder-summary").innerHTML = [
    insightCard("待执行", pending.length),
    insightCard("已归档", history.length),
  ].join("");

  renderEditableList($("#reminders-list"), pending, {
    fields: [
      { key: "session_id", extra: 'placeholder="会话 ID"' },
      { key: "run_at", extra: 'placeholder="YYYY-MM-DD HH:MM:SS"' },
      { key: "text", extra: 'placeholder="提醒内容"' },
    ],
    onSave: async (id, payload) => {
      await api(`/api/users/${encodeURIComponent(state.selectedUserId)}/simple-reminders/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    },
    onDelete: async (id) => {
      await api(`/api/users/${encodeURIComponent(state.selectedUserId)}/simple-reminders/${id}`, {
        method: "DELETE",
      });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    },
  });

  renderReminderHistory(history);
}

function fillStaticSelects() {
  renderSelect($("#alias-scene-type"), SCENE_OPTIONS, "global");
  renderSelect($("#preference-type-select"), PREFERENCE_OPTIONS, "like");
  renderSelect($("#bookkeeping-type-select"), BOOKKEEPING_TYPE_OPTIONS, "expense");
  renderSelect($("#relation-type-select"), RELATION_OPTIONS, "朋友");
  renderSelect($("#habit-module-select"), HABIT_MODULES, HABIT_MODULES[0].value);
  updateAliasSceneValueVisibility();
  updateHabitKeySelect();
}

function updateAliasSceneValueVisibility() {
  const sceneType = $("#alias-scene-type").value;
  const wrap = $("#alias-scene-value-wrap");
  const input = $("#alias-scene-value");
  const isGroup = sceneType === "group";
  wrap.classList.toggle("hidden", !isGroup);
  input.disabled = !isGroup;
  if (!isGroup) {
    input.value = "";
  }
}

function updateHabitKeySelect(selectedValue = "") {
  const moduleName = $("#habit-module-select").value || HABIT_MODULES[0].value;
  renderSelect($("#habit-key-select"), habitKeyOptions(moduleName), selectedValue, "请选择习惯类型");
}

function updateRelationTargetSelect(selectedValue = "") {
  renderSelect($("#relation-target-qq-id"), directoryUserOptions(), selectedValue, "请选择关系对象");
}

function renderDetail(detail) {
  state.detail = detail;
  $("#empty-state").classList.add("hidden");
  $("#detail-view").classList.remove("hidden");
  updateHero(detail);
  renderAliases(detail.user);
  renderGraph(detail.graph);
  updateRelationTargetSelect();

  renderEditableList($("#preferences-list"), detail.preferences || [], {
    fields: [
      { key: "preference_type", type: "select", options: PREFERENCE_OPTIONS },
      { key: "value" },
      { key: "confidence", extra: 'type="number" step="0.01" min="0" max="1"' },
      { key: "source_text" },
    ],
    onSave: async (id, payload) => {
      await api(`/api/preferences/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      await loadUser(state.selectedUserId);
    },
    onDelete: async (id) => {
      await api(`/api/preferences/${id}`, { method: "DELETE" });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    },
  });

  renderEditableList($("#relations-list"), detail.relations || [], {
    fields: [
      { key: "target_name" },
      { key: "relation_type", type: "select", options: RELATION_OPTIONS },
      { key: "confidence", extra: 'type="number" step="0.01" min="0" max="1"' },
      { key: "note" },
    ],
    onSave: async (id, payload) => {
      const current = detail.relations.find((item) => String(item.id) === String(id)) || {};
      payload.source_text = current.source_text || "记忆面板手动录入";
      payload.target_qq_id = current.target_qq_id || "";
      await api(`/api/relations/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      await loadUser(state.selectedUserId);
    },
    onDelete: async (id) => {
      await api(`/api/relations/${id}`, { method: "DELETE" });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    },
  });

  renderEditableList($("#habits-list"), detail.habits || [], {
    fields: [
      { key: "module_name", type: "select", options: HABIT_MODULES },
      { key: "habit_key", type: "select", options: (row) => habitKeyOptions(row.module_name) },
      { key: "habit_value" },
      { key: "source_text" },
    ],
    onSave: async (id, payload) => {
      await api(`/api/habits/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      await loadUser(state.selectedUserId);
    },
    onDelete: async (id) => {
      await api(`/api/habits/${id}`, { method: "DELETE" });
      await loadUser(state.selectedUserId);
      await loadOverview($("#user-search").value.trim());
    },
  });

  renderBookkeeping(detail.bookkeeping || {});
  renderReminders(detail.reminders || {});
  renderTimeline(detail.events || []);
}

async function loadOverview(query = "") {
  const payload = await api(`/api/overview?q=${encodeURIComponent(query)}`);
  state.overview = payload;
  renderStats(payload.stats);
  renderUsers(payload.users);
}

async function refreshDirectoryUsers() {
  const payload = await api("/api/users");
  state.directoryUsers = payload.users || [];
  updateRelationTargetSelect();
}

async function loadUser(userId) {
  state.selectedUserId = userId;
  const detail = await api(`/api/users/${encodeURIComponent(userId)}`);
  renderDetail(detail);
  renderUsers(state.overview?.users || []);
}

function bindForms() {
  $("#refresh-users").addEventListener("click", async () => {
    await refreshDirectoryUsers();
    await loadOverview($("#user-search").value.trim());
  });
  $("#user-search").addEventListener("input", (event) => loadOverview(event.target.value.trim()));
  $("#alias-scene-type").addEventListener("change", updateAliasSceneValueVisibility);
  $("#habit-module-select").addEventListener("change", () => updateHabitKeySelect());

  $("#profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const form = new FormData(formEl);
    const detail = await api(`/api/users/${state.selectedUserId}`, {
      method: "PUT",
      body: JSON.stringify(Object.fromEntries(form.entries())),
    });
    renderDetail(detail);
    await refreshDirectoryUsers();
    await loadOverview($("#user-search").value.trim());
  });

  $("#alias-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const payload = Object.fromEntries(new FormData(formEl).entries());
    if (payload.scene_type === "group" && !String(payload.scene_value || "").trim()) {
      throw new Error("群聊场景认人必须填写群号");
    }
    if (payload.scene_type !== "group") {
      payload.scene_value = "";
    }
    await api(`/api/users/${state.selectedUserId}/aliases`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resetForm(formEl);
    $("#alias-scene-type").value = "global";
    updateAliasSceneValueVisibility();
    await loadUser(state.selectedUserId);
    await refreshDirectoryUsers();
    await loadOverview($("#user-search").value.trim());
  });

  $("#preference-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const payload = Object.fromEntries(new FormData(formEl).entries());
    await api(`/api/users/${state.selectedUserId}/preferences`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resetForm(formEl);
    $("#preference-form [name='confidence']").value = "0.82";
    $(currentSourceText("#preference-form")).value = "记忆面板手动录入";
    await loadUser(state.selectedUserId);
    await loadOverview($("#user-search").value.trim());
  });

  $("#relation-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const payload = Object.fromEntries(new FormData(formEl).entries());
    if (!payload.target_qq_id) {
      throw new Error("请先从下拉框选择关系对象");
    }
    await api(`/api/users/${state.selectedUserId}/relations`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resetForm(formEl);
    $("#relation-form [name='confidence']").value = "0.84";
    $(currentSourceText("#relation-form")).value = "记忆面板手动录入";
    updateRelationTargetSelect();
    $("#relation-type-select").value = RELATION_OPTIONS[0].value;
    await loadUser(state.selectedUserId);
    await loadOverview($("#user-search").value.trim());
  });

  $("#habit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const payload = Object.fromEntries(new FormData(formEl).entries());
    if (!payload.habit_key) {
      throw new Error("请先选择习惯类型");
    }
    await api(`/api/users/${state.selectedUserId}/habits`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resetForm(formEl);
    $(currentSourceText("#habit-form")).value = "记忆面板手动录入";
    $("#habit-module-select").value = HABIT_MODULES[0].value;
    updateHabitKeySelect();
    await loadUser(state.selectedUserId);
    await loadOverview($("#user-search").value.trim());
  });

  $("#event-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const payload = Object.fromEntries(new FormData(formEl).entries());
    await api(`/api/users/${state.selectedUserId}/events`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resetForm(formEl);
    $("#event-form [name='confidence']").value = "0.82";
    $(currentSourceText("#event-form")).value = "记忆面板手动录入";
    await loadUser(state.selectedUserId);
    await loadOverview($("#user-search").value.trim());
  });

  $("#bookkeeping-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const payload = Object.fromEntries(new FormData(formEl).entries());
    await api(`/api/users/${encodeURIComponent(state.selectedUserId)}/bookkeeping`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resetForm(formEl);
    $("#bookkeeping-type-select").value = "expense";
    await loadUser(state.selectedUserId);
    await loadOverview($("#user-search").value.trim());
  });

  $("#reminder-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const formEl = event.currentTarget;
    const payload = Object.fromEntries(new FormData(formEl).entries());
    await api(`/api/users/${encodeURIComponent(state.selectedUserId)}/simple-reminders`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resetForm(formEl);
    await loadUser(state.selectedUserId);
    await loadOverview($("#user-search").value.trim());
  });
}

async function bootstrap() {
  if (!state.token) {
    $("#empty-state").classList.remove("hidden");
    $("#empty-state").innerHTML = "<div><p class='eyebrow'>访问受限</p><h2>当前链接缺少访问令牌</h2><p>请重新使用 bot 返回的完整面板链接打开。</p></div>";
    return;
  }
  fillStaticSelects();
  bindForms();
  await refreshDirectoryUsers();
  await loadOverview();
}

bootstrap().catch((error) => {
  $("#empty-state").classList.remove("hidden");
  $("#empty-state").innerHTML = `<div><p class="eyebrow">面板异常</p><h2>面板加载失败</h2><p>${escapeHtml(error.message)}</p></div>`;
});
