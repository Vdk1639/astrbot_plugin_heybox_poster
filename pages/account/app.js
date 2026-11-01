import { bridge } from "./bridge.js";
import { createPublishing } from "./publishing.js";
const state = {
  config: null,
  savedConfig: null,
  configSchema: null,
  status: null,
  loginTimer: null,
  loginRefreshTimer: null,
  sseId: null,
  sseConnecting: false,
  reconnectTimer: null,
  logRenderId: null,
  logs: [],
  eventPage: 1,
  eventPages: 1,
  eventId: "",
  eventBack: null,
  eventRangeInitialized: false,
  riskPage: 1,
  riskPages: 1,
  riskSelected: null,
  riskAuditPage: 1,
  riskAuditPages: 1,
  riskDetailLoaded: false,
  riskActionBusy: false,
  riskUncertain: false,
  toastTimer: null,
  confirmResolve: null,
  confirmPreviousFocus: null,
  unloaded: false,
  loginBusy: false,
  requests: {},
  sseRevision: 0,
};

const pendingButtons = new WeakSet();
const candidateActions = new Set();
const nextRequest = (key) => state.requests[key] = (state.requests[key] || 0) + 1;
const currentRequest = (key, version) => !state.unloaded && state.requests[key] === version;

const $ = (id) => document.getElementById(id);
const text = (value) => value === null || value === undefined || value === "" ? "—" : String(value);
const eventTypeLabels = {
  mention: "@ 提及",
  reply: "评论回复",
  proactive_feed: "主动 AI 回复",
};
const eventStatusLabels = {
  discovered: "已发现",
  claimed: "处理中",
  context_ready: "上下文就绪",
  dispatched: "已提交 AI",
  generated: "已生成",
  sent: "已发送",
  dry_run: "模拟运行",
  ignored: "已忽略",
  retry_wait: "等待重试",
  send_unknown: "发送状态未知",
  dead_letter: "处理失败",
};
const eventTypeText = (value) => eventTypeLabels[value] || text(value);
const eventStatusText = (value) => eventStatusLabels[value] || text(value);
const shanghaiTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});
const formatTime = (value) => {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = typeof value === "number" || /^\d+(?:\.\d+)?$/.test(String(value));
  const number = numeric ? Number(value) : Number.NaN;
  const date = numeric
    ? new Date(number < 1e12 ? number * 1000 : number)
    : new Date(String(value));
  if (Number.isNaN(date.getTime())) return text(value);
  const parts = Object.fromEntries(
    shanghaiTimeFormatter.formatToParts(date).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
};
const bytes = (value) => {
  const number = Number(value || 0);
  if (number < 1024) return `${number} B`;
  if (number < 1024 ** 2) return `${(number / 1024).toFixed(1)} KB`;
  return `${(number / 1024 ** 2).toFixed(1)} MB`;
};

function toast(message, tone = "info") {
  if (state.unloaded) return;
  const node = $("toast");
  if (state.toastTimer) clearTimeout(state.toastTimer);
  node.textContent = message;
  node.dataset.tone = tone;
  node.classList.add("show");
  state.toastTimer = setTimeout(() => {
    node.classList.remove("show");
    state.toastTimer = null;
  }, 3200);
}

function closeConfirmation(accepted) {
  const resolve = state.confirmResolve;
  state.confirmResolve = null;
  $("confirm-overlay").hidden = true;
  $("confirm-accept").classList.remove("danger");
  const previousFocus = state.confirmPreviousFocus;
  state.confirmPreviousFocus = null;
  previousFocus?.focus?.();
  resolve?.(accepted);
}

function confirmAction(message, { confirmText = "确认", danger = false } = {}) {
  if (state.unloaded) return Promise.resolve(false);
  if (state.confirmResolve) closeConfirmation(false);
  state.confirmPreviousFocus = document.activeElement;
  $("confirm-message").textContent = message;
  const accept = $("confirm-accept");
  accept.textContent = confirmText;
  accept.classList.toggle("danger", danger);
  $("confirm-overlay").hidden = false;
  return new Promise((resolve) => {
    state.confirmResolve = resolve;
    queueMicrotask(() => accept.focus());
  });
}

async function busy(button, work) {
  if (state.unloaded || pendingButtons.has(button)) return;
  pendingButtons.add(button);
  button.disabled = true;
  try { await bridge.ready(); return await work(); }
  catch (error) { toast(error.message, "error"); return undefined; }
  finally { pendingButtons.delete(button); if (!state.unloaded) button.disabled = false; }
}

function metric(label, value) {
  const card = document.createElement("article");
  card.className = "metric";
  const labelNode = document.createElement("div");
  labelNode.className = "label";
  labelNode.textContent = label;
  const valueNode = document.createElement("div");
  valueNode.className = "value";
  valueNode.textContent = text(value);
  card.append(labelNode, valueNode);
  return card;
}

function notificationMetric(profile, eventType) {
  const poll = profile.notification_polls?.[eventType];
  if (!poll) return "—";
  return `${Number(poll.raw_count || 0)} / ${Number(poll.accepted_count || 0)}`;
}

function table(headers, rows) {
  const tableNode = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((header) => {
    const cell = document.createElement("th");
    cell.textContent = header;
    headRow.append(cell);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    row.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === row.length - 1) cell.className = "wrap";
      if (value instanceof Node) cell.append(value);
      else cell.textContent = text(value);
      tr.append(cell);
    });
    body.append(tr);
  });
  tableNode.append(head, body);
  return tableNode;
}

function selectedProfile() {
  return $("login-profile").value || "default";
}

async function loadConfig() {
  const version = nextRequest("config");
  const [config, schema] = await Promise.all([
    bridge.apiGet("config"),
    bridge.apiGet("config/schema"),
  ]);
  if (!currentRequest("config", version)) return;
  state.config = config;
  state.savedConfig = structuredClone(config);
  state.configSchema = schema;
  renderConfigForm();
  populateProfileSelect();
  publishing.configure(config);
  $("event-profile-options").replaceChildren(...(config.profiles || []).map((profile) => {
    const option = document.createElement("option");
    option.value = profile.profile_id;
    option.textContent = profile.display_name || profile.profile_id;
    return option;
  }));
  updateRiskActions();
}

function populateProfileSelect() {
  const select = $("login-profile");
  const previous = select.value;
  select.replaceChildren();
  (state.config.profiles || []).forEach((profile) => {
    const option = document.createElement("option");
    option.value = profile.profile_id;
    option.textContent = `${profile.display_name || profile.profile_id} · ${profile.profile_id}`;
    select.append(option);
  });
  if ([...select.options].some((option) => option.value === previous)) {
    select.value = previous;
  }
}

function schemaDefault(descriptor) {
  if (Object.hasOwn(descriptor, "default")) {
    return structuredClone(descriptor.default);
  }
  if (descriptor.type === "bool") return false;
  if (descriptor.type === "int" || descriptor.type === "float") return 0;
  if (descriptor.type === "list") return [];
  if (descriptor.type === "object") {
    return Object.fromEntries(
      Object.entries(descriptor.items || {}).map(([key, item]) => [key, schemaDefault(item)]),
    );
  }
  return "";
}

function fieldLabel(descriptor, key) {
  return descriptor.description || key;
}

function appendHint(host, descriptor) {
  if (!descriptor.hint) return;
  const hint = document.createElement("small");
  hint.className = "config-hint";
  hint.textContent = descriptor.hint;
  host.append(hint);
}

function createScalarField(key, descriptor, value, update) {
  if (descriptor.type === "bool") {
    const row = document.createElement("label");
    row.className = "config-toggle";
    const copy = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = fieldLabel(descriptor, key);
    copy.append(title);
    appendHint(copy, descriptor);
    const control = document.createElement("input");
    control.type = "checkbox";
    control.checked = Boolean(value);
    control.addEventListener("change", () => update(control.checked));
    row.append(copy, control);
    return row;
  }

  const field = document.createElement("label");
  field.className = "config-field";
  const title = document.createElement("span");
  title.className = "config-label";
  title.textContent = fieldLabel(descriptor, key);
  field.append(title);

  let control;
  if (descriptor.type === "list") {
    control = document.createElement("textarea");
    control.className = "config-list";
    control.rows = 4;
    control.value = Array.isArray(value) ? value.map(String).join("\n") : "";
    control.placeholder = "每行一项";
    control.addEventListener("input", () => {
      update(control.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean));
    });
  } else if (descriptor.type === "string" && Array.isArray(descriptor.options)) {
    control = document.createElement("select");
    descriptor.options.forEach((optionValue) => {
      const option = document.createElement("option");
      const value = typeof optionValue === "object" ? optionValue.value : optionValue;
      option.value = String(value);
      option.textContent = String(
        (typeof optionValue === "object" && optionValue.label)
          || descriptor.option_labels?.[value]
          || value,
      );
      control.append(option);
    });
    control.value = String(value ?? descriptor.default ?? "");
    control.addEventListener("change", () => update(control.value));
  } else {
    control = document.createElement("input");
    const numeric = descriptor.type === "int" || descriptor.type === "float";
    control.type = numeric ? "number" : "text";
    control.value = value ?? descriptor.default ?? "";
    if (numeric) {
      const slider = descriptor.slider || {};
      const minimum = descriptor.min ?? slider.min;
      const maximum = descriptor.max ?? slider.max;
      if (minimum !== undefined) control.min = String(minimum);
      if (maximum !== undefined) control.max = String(maximum);
      control.step = String(slider.step ?? (descriptor.type === "int" ? 1 : 0.1));
      const readNumber = () => {
        const parsed = Number(control.value);
        if (control.value.trim() && Number.isFinite(parsed)
          && (descriptor.type !== "int" || Number.isInteger(parsed))
          && (minimum === undefined || parsed >= minimum)
          && (maximum === undefined || parsed <= maximum)) {
          update(parsed);
          control.setCustomValidity("");
        } else {
          control.setCustomValidity("请输入范围内的有效数字（整数配置不接受小数）");
          control.reportValidity();
        }
      };
      control.addEventListener("input", readNumber);
      control.addEventListener("change", readNumber);
    } else {
      control.addEventListener("input", () => update(control.value));
    }
  }
  control.dataset.configKey = key;
  field.append(control);
  appendHint(field, descriptor);
  return field;
}

function profileTemplate(schema) {
  const entries = Object.entries(schema.templates || {});
  if (!entries.length) throw new Error("账号档案界面定义缺少模板");
  const [templateKey, template] = entries[0];
  const profile = { __template_key: templateKey };
  Object.entries(template.items || {}).forEach(([key, descriptor]) => {
    profile[key] = schemaDefault(descriptor);
  });
  return { template, profile };
}

function nextProfileId() {
  const used = new Set((state.config.profiles || []).map((item) => String(item.profile_id)));
  let number = 2;
  while (used.has(`profile_${number}`)) number += 1;
  return `profile_${number}`;
}

function renderProfiles(schema) {
  const wrapper = document.createElement("details");
  wrapper.className = "config-section";
  wrapper.open = true;
  const summary = document.createElement("summary");
  const heading = document.createElement("span");
  heading.textContent = schema.description || "账号档案";
  const count = document.createElement("small");
  count.textContent = `${(state.config.profiles || []).length} 个档案`;
  summary.append(heading, count);
  wrapper.append(summary);
  appendHint(wrapper, schema);

  const profilesHost = document.createElement("div");
  profilesHost.className = "profile-cards";
  const { template } = profileTemplate(schema);
  (state.config.profiles || []).forEach((profile, index) => {
    const card = document.createElement("article");
    card.className = "profile-card";
    const cardHeading = document.createElement("div");
    cardHeading.className = "profile-heading";
    const title = document.createElement("h3");
    title.textContent = profile.display_name || profile.profile_id || `账号 ${index + 1}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger secondary compact";
    remove.textContent = "删除档案";
    remove.disabled = state.config.profiles.length <= 1;
    remove.addEventListener("click", async () => {
      const name = profile.profile_id || index + 1;
      const confirmed = await confirmAction(
        `即将只删除账号档案 ${name}。如需同时清除登录凭证，请先取消并在扫码登录页执行安全退出。确认继续？`,
        { confirmText: "删除档案", danger: true },
      );
      if (!confirmed) return;
      state.config.profiles.splice(index, 1);
      renderConfigForm();
    });
    cardHeading.append(title, remove);
    const grid = document.createElement("div");
    grid.className = "config-grid";
    Object.entries(template.items || {}).forEach(([key, descriptor]) => {
      grid.append(createScalarField(key, descriptor, profile[key], (nextValue) => {
        profile[key] = nextValue;
      }));
    });
    card.append(cardHeading, grid);
    profilesHost.append(card);
  });
  const add = document.createElement("button");
  add.type = "button";
  add.className = "secondary add-profile";
  add.textContent = "添加账号档案";
  add.addEventListener("click", () => {
    const { profile } = profileTemplate(schema);
    profile.profile_id = nextProfileId();
    profile.display_name = `账号 ${state.config.profiles.length + 1}`;
    state.config.profiles.push(profile);
    renderConfigForm();
  });
  wrapper.append(profilesHost, add);
  return wrapper;
}

function renderObjectGroup(groupKey, schema) {
  const wrapper = document.createElement("details");
  wrapper.className = "config-section";
  wrapper.open = true;
  const summary = document.createElement("summary");
  const heading = document.createElement("span");
  heading.textContent = schema.description || groupKey;
  const key = document.createElement("small");
  key.textContent = groupKey;
  summary.append(heading, key);
  const grid = document.createElement("div");
  grid.className = "config-grid";
  const group = state.config[groupKey] || (state.config[groupKey] = {});
  Object.entries(schema.items || {}).forEach(([fieldKey, descriptor]) => {
    grid.append(createScalarField(fieldKey, descriptor, group[fieldKey], (nextValue) => {
      group[fieldKey] = nextValue;
    }));
  });
  wrapper.append(summary, grid);
  return wrapper;
}

function renderConfigForm() {
  const host = $("config-form");
  host.replaceChildren();
  if (!state.config || !state.configSchema) return;
  Object.entries(state.configSchema).forEach(([groupKey, schema]) => {
    if (schema.type === "template_list" && groupKey === "profiles") {
      host.append(renderProfiles(schema));
    } else if (schema.type === "object") {
      host.append(renderObjectGroup(groupKey, schema));
    }
  });
}

async function loadStatus() {
  const version = nextRequest("status");
  const result = await bridge.apiGet("status");
  if (!currentRequest("status", version)) return;
  state.status = result;
  const profiles = state.status.profiles || [];
  const first = profiles[0] || {};
  const cards = $("status-cards");
  cards.replaceChildren(
    metric("插件版本", state.status.version),
    metric("登录状态", first.status || "idle"),
    metric("当前账号", first.nickname || first.profile_id),
    metric("UID", first.uid),
    metric("最后轮询", formatTime(first.last_poll_at)),
    metric("最近成功请求", formatTime(first.last_success_request_at)),
    metric("最近错误", first.last_error || first.last_client_error?.message),
    metric("@通知 原始/接收", notificationMetric(first, "mention")),
    metric("回复通知 原始/接收", notificationMetric(first, "reply")),
    metric("待处理队列", state.status.queue_length || 0),
    metric("今日回复", first.reply_count || 0),
    metric("今日主动 AI 请求", first.proactive_count || 0),
    metric("模拟运行", first.dry_run ? "开启" : "关闭"),
    metric("数据库", bytes(state.status.database_size)),
    metric("日志", bytes(state.status.log_size)),
    metric("后台任务", (state.status.tasks || []).length),
    metric("辅助模型冷却", (state.status.provider_cooldowns || []).length),
    metric(
      "熔断状态",
      Number(first.circuit_open_until || 0) > Date.now() / 1000 ? "已打开" : "正常",
    ),
  );
  $("reply-budgets").replaceChildren(...profiles.flatMap((profile) => {
    const budget = profile.reply_budget;
    const label = profile.display_name || profile.profile_id || "账号";
    if (!budget) return [metric(`${label} · 今日额度`, "未提供")];
    const limit = (value) => value === 0 ? "不限" : text(value);
    return [
      metric(`${label} · 今日通知处理`, `${text(budget.processing_count)} / ${limit(budget.processing_limit)}`),
      metric(`${label} · 今日真实评论`, `${text(budget.comment_count)} / ${limit(budget.comment_limit)}`),
      metric(`${label} · 重置 / 缺 UID`, `北京时间 ${budget.reset_local_time || "08:00"}（UTC 0 点） / ${budget.unverified_shared ? "共用预算桶" : "未提供"}`),
    ];
  }));
  const adapters = state.status.adapters || [];
  const adapterHost = $("adapter-list");
  adapterHost.replaceChildren(table(
    ["实例 ID", "profile_id", "运行状态"],
    adapters.map((item) => [item.id, item.profile_id, item.running ? "运行中" : "停止"]),
  ));
  renderProviderCooldowns();
  const alerts = $("alerts");
  alerts.replaceChildren();
  (state.status.alerts || []).forEach((item) => {
    const node = document.createElement("div");
    node.className = "alert";
    node.textContent = item.message;
    alerts.append(node);
  });
}

function renderProviderCooldowns() {
  const host = $("provider-cooldown-list");
  const cooldowns = state.status.provider_cooldowns || [];
  if (!cooldowns.length) {
    host.textContent = "当前没有辅助模型处于冷却状态。";
    return;
  }
  const tableNode = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["账号", "用途", "Provider", "剩余", "最近错误", "操作"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.append(cell);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  cooldowns.forEach((item) => {
    const row = document.createElement("tr");
    const purpose = item.purpose === "image" ? "图片转述" : "楼层上下文压缩";
    [
      item.profile_id,
      purpose,
      item.provider_id,
      `${Number(item.remaining_seconds || 0)} 秒`,
      item.last_error,
    ].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = text(value);
      row.append(cell);
    });
    const actionCell = document.createElement("td");
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "secondary";
    clear.textContent = "结束冷却";
    clear.addEventListener("click", () => busy(clear, async () => {
      await bridge.apiPost("status/provider-cooldown/clear", {
        profile_id: item.profile_id,
        purpose: item.purpose,
        provider_id: item.provider_id,
      });
      await loadStatus();
      toast("已结束辅助模型冷却；下次需要时会重新尝试。", "success");
    }));
    actionCell.append(clear);
    row.append(actionCell);
    body.append(row);
  });
  tableNode.append(head, body);
  host.replaceChildren(tableNode);
}

async function loadLogin() {
  if (state.loginBusy || state.unloaded) return;
  const profile = selectedProfile();
  const version = nextRequest("login");
  try {
    const result = await bridge.apiGet("auth/status", { profile_id: profile });
    if (currentRequest("login", version) && profile === selectedProfile()) renderLogin(result);
  } catch (error) {
    if (currentRequest("login", version)) {
      toast(`登录状态刷新失败：${error.message}`, "error");
      clearTimeout(state.loginRefreshTimer);
      state.loginRefreshTimer = setTimeout(loadLogin, 3000);
    }
  }
}

function clearLogin() {
  clearInterval(state.loginTimer);
  clearTimeout(state.loginRefreshTimer);
  $("qr-image").removeAttribute("src");
  $("qr-image").hidden = true;
  $("qr-placeholder").hidden = false;
  $("qr-countdown").textContent = "";
}

async function loginAction(action) {
  if (state.loginBusy || state.unloaded) return;
  state.loginBusy = true;
  const profile = selectedProfile();
  const version = nextRequest("login");
  const controls = ["request-qr", "check-login", "logout", "login-profile"].map($);
  controls.forEach((control) => { control.disabled = true; });
  clearLogin();
  try {
    if (action === "logout" && !await confirmAction(`确认删除账号档案 ${profile} 的本地凭证并退出？`, { confirmText: "安全退出", danger: true })) return;
    if (!currentRequest("login", version) || profile !== selectedProfile()) return;
    const result = await bridge.apiPost(`auth/${action}`, { profile_id: profile, ...(action === "logout" ? { confirm: true } : {}) });
    if (!currentRequest("login", version) || profile !== selectedProfile()) return;
    renderLogin(result);
    await Promise.all([loadStatus(), publishing.loadStatus()]);
  } catch (error) {
    if (currentRequest("login", version)) toast(`账号操作失败：${error.message}`, "error");
  } finally {
    state.loginBusy = false;
    if (!state.unloaded) {
      controls.forEach((control) => { control.disabled = false; });
      await loadLogin();
    }
  }
}

function renderLogin(result) {
  const facts = $("login-facts");
  const values = {
    "状态": result.state,
    "昵称": result.nickname,
    "UID": result.uid,
    "登录时间": formatTime(result.logged_in_at),
    "最近检查": formatTime(result.last_login_check_at),
  };
  facts.replaceChildren();
  Object.entries(values).forEach(([key, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = text(value);
    facts.append(dt, dd);
  });
  clearLogin();
  const activeStates = new Set(["requesting_qr", "waiting_scan", "scanned_waiting_confirm"]);
  const expiresAt = Number(result.expires_at || 0);
  if (activeStates.has(result.state) && /^data:image\/png;base64,[a-z\d+/=]+$/i.test(result.qr_image || "") && (!expiresAt || expiresAt > Date.now() / 1000)) {
    $("qr-image").src = result.qr_image;
    $("qr-image").hidden = false;
    $("qr-placeholder").hidden = true;
  } else {
    $("qr-image").removeAttribute("src");
    $("qr-image").hidden = true;
    $("qr-placeholder").hidden = false;
  }
  if (result.expires_at) {
    const tick = () => {
      if (state.unloaded) return;
      const remaining = Math.max(0, Math.floor(result.expires_at - Date.now() / 1000));
      $("qr-countdown").textContent = remaining ? `二维码剩余 ${remaining} 秒` : "二维码已过期";
      if (!remaining) {
        clearInterval(state.loginTimer);
        $("qr-image").removeAttribute("src");
        $("qr-image").hidden = true;
        $("qr-placeholder").hidden = false;
      }
    };
    tick();
    if (expiresAt > Date.now() / 1000) state.loginTimer = setInterval(tick, 500);
  } else {
    $("qr-countdown").textContent = "";
  }
  clearTimeout(state.loginRefreshTimer);
  if (activeStates.has(result.state) && !state.unloaded) {
    state.loginRefreshTimer = setTimeout(async () => {
      try { await loadLogin(); }
      catch (error) { toast(`登录状态刷新失败：${error.message}`); }
    }, 3000);
  }
}

async function loadEvents() {
  const version = nextRequest("events");
  const start = $("event-start").value;
  const end = $("event-end").value;
  const grouped = $("event-mode").value === "uid";
  const result = await bridge.apiGet("events", {
    group_by: $("event-mode").value,
    profile_id: $("event-profile").value.trim(),
    event_id: state.eventId,
    status: $("event-status").value,
    uid: $("event-uid").value.trim(),
    post_id: $("event-post").value.trim(),
    keyword: $("event-keyword").value.trim(),
    start_time: start ? Date.parse(start) / 1000 : "",
    end_time: end ? Date.parse(end) / 1000 : "",
    page: state.eventPage,
    page_size: 50,
  });
  if (!currentRequest("events", version)) return;
  state.eventPages = Math.max(1, Math.ceil(result.total / result.page_size));
  $("event-page").textContent = `第 ${result.page} / ${state.eventPages} 页，共 ${result.total} ${grouped ? "组用户记录" : "条"}`;
  $("event-prev").disabled = result.page <= 1;
  $("event-next").disabled = result.page >= state.eventPages;
  $("event-range-hint").textContent = `${start || "不限开始"} 至 ${end || "不限结束"}${state.eventId ? ` · 单事件 ID ${state.eventId}` : ""}。输入为浏览器本地时间，列表显示北京时间；统计仅含当前筛选结果。`;
  if (grouped) {
    $("event-table").replaceChildren(table(
      ["档案 / UID", "最新昵称", "事件 / 帖子", "状态 / 类型分布", "最近处理 / 发现", "当前风险", "操作"],
      result.items.map((item) => [
        `${item.profile_id} / ${item.identity_verified === false || !item.sender_uid ? "UID 未提供" : item.sender_uid}`,
        item.sender_nickname,
        `${item.event_count} / ${item.post_count}`,
        `${distribution(item.status_counts, eventStatusText)}\n${distribution(item.event_type_counts, eventTypeText)}`,
        `${formatTime(item.last_event_at)}\n${formatTime(item.last_discovered_at)}`,
        riskStatus(item), userActions(item),
      ]),
    ));
  } else $("event-table").replaceChildren(table(
    ["时间", "档案", "类型", "状态", "UID", "帖子 / 楼层", "风险评估 / 行为资格", "内容 / 结果"],
    result.items.map((item) => [
      formatTime(item.event_time || item.discovered_at),
      item.profile_id,
      eventTypeText(item.event_type),
      eventStatusText(item.status),
      item.sender_uid || "UID 未提供（不能可靠归属或封禁）",
      `${item.post_id} / ${item.root_comment_id || "帖子"}`,
      eventRiskText(item),
      `${item.content || ""}\n${item.reply_text ? `→ ${item.reply_text}` : ""}\n${item.error || ""}`.trim(),
    ]),
  ));
}

function eventRiskText(item) {
  const state = String(item.risk_state || "");
  const label = {
    evaluated: "已评估", pending: "待评估", skipped_legacy: "旧库未评估（不回评）",
    skipped_disabled: "永久跳过（评分关闭）", skipped_source: "永久跳过（非通知来源）",
    skipped_uid: "永久跳过（缺 UID）", skipped_self: "永久跳过（自身消息）",
    skipped_owner: "永久跳过（主人豁免）",
  }[state] || (state.startsWith("skipped_") ? `永久跳过（${state}）` : state || "未评估（旧记录或 API 未提供状态）");
  const eligible = item.risk_behavior_eligible;
  const behavior = eligible === false || eligible === 0 ? "历史回填或时间不明，未计短时风险"
    : eligible === true || eligible === 1 ? "具备短时行为资格" : "短时行为资格未提供";
  const evaluation = state === "evaluated"
    ? ` · ${formatTime(item.risk_evaluated_at)} · 本事件增分 ${text(item.risk_delta)} · 评估后分数 ${text(item.risk_score_after)} · 命中 ${Array.isArray(item.risk_hits) ? item.risk_hits.join("、") || "无" : text(item.risk_hits)}`
    : "";
  return `${label}${evaluation}\n${behavior}${item.reason ? `\n原因：${item.reason}` : ""}`;
}

function distribution(counts, label) {
  return Object.entries(counts || {}).map(([key, value]) => `${label(key)} ${value}`).join("、") || "—";
}

function riskStatus(user) {
  const score = user.score ?? user.risk_score;
  if (user.manual_blocked) return `${text(score)} 分 · 人工封禁`;
  if (user.has_record === false) return "无风险档案（不表示已评估全部事件）";
  if (score === null || score === undefined) return "未启用/未接入";
  const level = user.level || user.risk_level;
  const status = user.auto_filtered ? "自动过滤生效"
    : level === "blocked" ? "已达阈值，自动过滤未生效" : level === "watch" ? "观察" : "正常";
  return `${score} 分 · ${status}`;
}

function userActions(user) {
  const row = document.createElement("div");
  row.className = "button-row";
  const drill = document.createElement("button");
  drill.type = "button";
  drill.textContent = "查看事件";
  drill.addEventListener("click", () => busy(drill, () => drillEvents(user)));
  const detail = document.createElement("button");
  detail.type = "button";
  detail.className = "secondary";
  detail.textContent = "风险记录";
  detail.disabled = user.identity_verified === false || !String(user.sender_uid || "").trim();
  detail.addEventListener("click", () => busy(detail, () => loadRiskDetail(user.profile_id, user.sender_uid)));
  row.append(drill, detail);
  if (detail.disabled) {
    const hint = document.createElement("span");
    hint.textContent = "UID 未提供，不能可靠归属；仅可查看单事件";
    row.append(hint);
  }
  return row;
}

const eventFilterIds = ["event-mode", "event-profile", "event-status", "event-uid", "event-post", "event-keyword", "event-start", "event-end"];

async function drillEvents(user) {
  if (!state.eventBack) state.eventBack = {
    page: state.eventPage,
    values: eventFilterIds.map((id) => $(id).value),
  };
  $("event-mode").value = "";
  $("event-profile").value = user.profile_id;
  $("event-uid").value = user.identity_verified === false ? "" : user.sender_uid || "";
  state.eventId = user.identity_verified === false || !user.sender_uid ? user.event_id : "";
  state.eventPage = 1;
  $("event-back").hidden = false;
  closeRiskDetail();
  nextRequest("risk-users");
  await Promise.all([loadEvents(), loadRiskUsers()]);
}

async function loadRiskUsers() {
  const version = nextRequest("risk-users");
  const result = await bridge.apiGet("risk/users", {
    profile_id: $("event-profile").value.trim(), uid: $("risk-uid").value.trim(),
    level: $("risk-level").value, manual_blocked: $("risk-manual").value,
    page: state.riskPage, page_size: 30,
  });
  if (!currentRequest("risk-users", version)) return;
  state.riskPages = Math.max(1, Math.ceil(result.total / result.page_size));
  $("risk-page").textContent = `第 ${result.page} / ${state.riskPages} 页，共 ${result.total} 条风险档案`;
  $("risk-prev").disabled = result.page <= 1;
  $("risk-next").disabled = result.page >= state.riskPages;
  $("risk-users").replaceChildren(table(["档案 / UID", "当前风险", "操作"], result.items.map((user) => [
    `${user.profile_id} / ${user.sender_uid}`, riskStatus(user), userActions(user),
  ])));
}

function closeRiskDetail() {
  nextRequest("risk-detail");
  state.riskSelected = null;
  state.riskDetailLoaded = false;
  if (state.confirmResolve && state.riskActionBusy) closeConfirmation(false);
  $("risk-detail").hidden = true;
  updateRiskActions();
}

function updateRiskActions() {
  const currentProfile = (state.savedConfig?.profiles || []).some((profile) => profile.profile_id === state.riskSelected?.profile_id);
  const disabled = !state.riskDetailLoaded || !currentProfile || state.riskActionBusy || state.riskUncertain;
  for (const id of ["risk-block", "risk-unblock", "risk-adjust"]) $(id).disabled = disabled;
  for (const id of ["refresh-risk", "risk-audit-prev", "risk-audit-next"]) {
    $(id).disabled = !state.riskSelected || state.riskActionBusy
      || id === "risk-audit-prev" && state.riskAuditPage <= 1
      || id === "risk-audit-next" && state.riskAuditPage >= state.riskAuditPages;
  }
}

async function loadRiskDetail(profile_id, uid, page = 1) {
  const changedUser = state.riskSelected?.profile_id !== profile_id || state.riskSelected?.uid !== uid;
  if (changedUser) {
    $("risk-operation-note").value = "";
    $("risk-reset").checked = false;
    $("risk-delta").value = "0";
  }
  const version = nextRequest("risk-detail");
  state.riskSelected = { profile_id, uid };
  state.riskDetailLoaded = false;
  $("risk-detail").hidden = false;
  $("risk-facts").replaceChildren();
  $("risk-audit").replaceChildren();
  $("risk-note").textContent = "";
  $("risk-result").textContent = "正在加载风险详情…";
  updateRiskActions();
  const result = await bridge.apiGet("risk/users/detail", { profile_id, uid, page, page_size: 30 });
  if (!currentRequest("risk-detail", version)) return;
  const user = result.user;
  const audit = result.events;
  if (!user || user.profile_id !== profile_id || user.sender_uid !== uid || !audit) throw new Error("风险详情身份或返回结构不匹配");
  state.riskAuditPage = audit.page;
  state.riskAuditPages = Math.max(1, Math.ceil(audit.total / audit.page_size));
  state.riskDetailLoaded = true;
  state.riskUncertain = false;
  const policy = state.savedConfig?.risk;
  $("risk-facts").replaceChildren(
    metric("档案 / UID", `${profile_id} / ${uid}`), metric("当前风险", riskStatus(user)),
    metric("自动评分", policy ? policy.enabled ? "开启" : "关闭" : "未接入"),
    metric("自动过滤开关", policy ? policy.auto_filter_enabled ? "开启" : "关闭" : "未接入"),
    metric("人工封禁", user.manual_blocked ? "生效（独立于评分开关）" : "未封禁"),
    metric("最近命中", formatTime(user.last_hit_at)),
    metric("最近更新", formatTime(user.updated_at)),
  );
  $("risk-note").textContent = `备注：${user.note || "无"}`;
  $("risk-result").textContent = user.manual_blocked || user.auto_filtered
    ? "当前风险策略阻止处理；其他硬名单和限额仍独立生效。"
    : "当前未被风险策略拦截；硬名单和处理/发送限额仍可能阻止。";
  if (!user.manual_blocked && user.auto_filtered) $("risk-result").textContent = "当前无人工封禁，但风险阈值仍阻止处理；硬名单和限额仍独立生效。";
  if (!(state.savedConfig?.profiles || []).some((profile) => profile.profile_id === profile_id)) $("risk-result").textContent += " 历史档案仅供查看。";
  $("risk-audit").replaceChildren(table(["时间", "关联事件", "规则 / 操作", "实际增减", "分数变化", "操作者 / 详情"], audit.items.map((item) => [
    formatTime(item.created_at), item.incoming_event_id, item.rule, item.delta, `${text(item.before_score)} → ${text(item.after_score)}`,
    `${item.operator || "自动"}\n${typeof item.details === "object" ? JSON.stringify(item.details) : item.details || item.detail || ""}`,
  ])));
  $("risk-audit-page").textContent = `第 ${audit.page} / ${state.riskAuditPages} 页，共 ${audit.total} 条审计`;
  $("risk-audit-prev").disabled = audit.page <= 1;
  $("risk-audit-next").disabled = audit.page >= state.riskAuditPages;
  updateRiskActions();
}

async function changeRisk(action) {
  if (!state.riskSelected || !state.riskDetailLoaded || state.riskActionBusy || state.riskUncertain) return;
  if (!(state.savedConfig?.profiles || []).some((profile) => profile.profile_id === state.riskSelected.profile_id)) return;
  const target = { ...state.riskSelected };
  const version = state.requests["risk-detail"];
  const note = $("risk-operation-note").value;
  const deltaText = $("risk-delta").value.trim();
  const delta = Number(deltaText);
  const reset_score = $("risk-reset").checked;
  if (note.length > 200) throw new Error("备注不能超过 200 字");
  if (action === "adjust" && (!deltaText || !Number.isInteger(delta) || delta < -1000 || delta > 1000)) throw new Error("调整分数须为 -1000..1000 的整数");
  state.riskActionBusy = true;
  updateRiskActions();
  let attempted = false;
  try {
    const label = { block: "人工封禁", unblock: "解除人工封禁", adjust: "调整分数" }[action];
    const accepted = await confirmAction(`确认${label}？\n档案 ${target.profile_id} · UID ${target.uid}\n备注：${note || "无"}\n${action === "adjust" ? `调整 ${delta} 分` : action === "unblock" ? `清零分数：${reset_score ? "是" : "否"}；不移除硬黑名单` : "人工封禁独立于评分开关"}`, { confirmText: label, danger: action === "block" });
    if (!accepted || !currentRequest("risk-detail", version)) return;
    attempted = true;
    await bridge.apiPost(`risk/users/${action}`, { ...target, note, confirm: true,
      ...(action === "adjust" ? { delta } : action === "unblock" ? { reset_score } : {}),
    });
    if (!currentRequest("risk-detail", version)) return;
    toast("风险操作已记录；解除人工封禁不代表其他过滤原因已解除。");
    await Promise.all([loadRiskDetail(target.profile_id, target.uid), loadRiskUsers(), loadEvents()]);
  } catch (error) {
    if (currentRequest("risk-detail", version)) {
      state.riskUncertain = attempted;
      $("risk-result").textContent = attempted ? "操作结果未确认；请刷新详情并核对审计后再操作，勿直接重复调分。" : error.message;
    }
    throw error;
  } finally {
    state.riskActionBusy = false;
    if (!state.unloaded) updateRiskActions();
  }
}

async function loadCandidates() {
  const version = nextRequest("candidates");
  const [pending, unknown] = await Promise.all([
    bridge.apiGet("feed/candidates", { status: "pending", limit: 100 }),
    bridge.apiGet("feed/candidates", { status: "send_unknown", limit: 100 }),
  ]);
  if (!currentRequest("candidates", version)) return;
  const items = [...(unknown.items || []), ...(pending.items || [])];
  const host = $("candidate-list");
  host.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.textContent = "待审核候选为 0 条。";
    host.append(empty);
    return;
  }
  items.forEach((candidate) => {
    const card = document.createElement("article");
    card.className = "candidate";
    const title = document.createElement("h3");
    title.textContent = candidate.post_title || `帖子 ${candidate.post_id}`;
    const meta = document.createElement("p");
    const statusText = candidate.status === "send_unknown" ? "发送状态未知，请人工核对" : "待审核";
    meta.textContent = `档案 ${candidate.profile_id || "—"} · 目标帖子 ${candidate.post_id} · 作者 UID ${candidate.post_author_uid || "未知"} · ${statusText} · ${candidate.reason || "AI 候选"}`;
    const editor = document.createElement("textarea");
    editor.value = candidate.edited_text || candidate.generated_text;
    editor.setAttribute("aria-label", `帖子 ${candidate.post_id} 的候选评论`);
    editor.readOnly = candidate.status === "send_unknown";
    const row = document.createElement("div");
    row.className = "button-row";
    if (candidate.status === "send_unknown") {
      const warning = document.createElement("p");
      warning.className = "review-warning";
      warning.textContent = "发送结果未知，请先核对目标帖子，不能重发。";
      card.append(title, meta, editor, warning);
      host.append(card);
      return;
    }
    if (candidate.status !== "pending") {
      editor.readOnly = true;
      card.append(title, meta, editor);
      host.append(card);
      return;
    }
    const approve = document.createElement("button");
    approve.type = "button";
    approve.textContent = "批准";
    approve.addEventListener("click", () => candidateAction(approve, reject, candidate, async () => {
      const simulated = Boolean(state.config?.proactive_feed?.dry_run);
      const editedText = editor.value;
      const confirmed = await confirmAction(
        simulated
          ? "当前为主动浏览模拟运行。确认保存这条批准结果？"
          : `确认使用档案 ${candidate.profile_id} 将以下评论发送到帖子 ${candidate.post_id}？\n${editedText}`,
        { confirmText: "批准" },
      );
      if (!confirmed) return false;
      nextRequest("candidates");
      const result = await bridge.apiPost(
        `feed/candidates/${encodeURIComponent(candidate.id)}/approve`,
        { edited_text: editedText, confirm: true },
      );
      toast(result.result === "queued" ? "已转入统一评论审核，请核对账号后批准" : result.result === "dry_run" ? "模拟运行候选已批准" : result.result === "sent" || result.status === "sent" ? "候选已发送" : "请刷新候选状态并人工核对；发送结果未知时不能重发。");
      await loadCandidates();
      return true;
    }));
    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "danger secondary";
    reject.textContent = "拒绝";
    reject.addEventListener("click", () => candidateAction(approve, reject, candidate, async () => {
      nextRequest("candidates");
      await bridge.apiPost(`feed/candidates/${encodeURIComponent(candidate.id)}/reject`, {});
      toast("候选已拒绝");
      await loadCandidates();
      return true;
    }));
    row.append(approve, reject);
    card.append(title, meta, editor, row);
    host.append(card);
  });
}

async function candidateAction(approve, reject, candidate, work) {
  if (state.unloaded || candidateActions.has(candidate.id) || candidate.status !== "pending") return;
  candidateActions.add(candidate.id);
  approve.disabled = reject.disabled = true;
  let attempted = true;
  try { attempted = await work(); }
  catch { toast("候选操作结果未确认，请刷新列表并核对目标帖子；不能直接重发。", "error"); }
  finally {
    candidateActions.delete(candidate.id);
    if (!state.unloaded) approve.disabled = reject.disabled = attempted;
  }
}

async function loadLogs() {
  const version = nextRequest("logs");
  const result = await bridge.apiGet("logs", {
    level: $("log-level").value,
    keyword: $("log-keyword").value.trim(),
    limit: 300,
  });
  if (!currentRequest("logs", version)) return;
  state.logs = result.items.reverse();
  renderLogs();
}

function renderLogs() {
  if (state.unloaded) return;
  $("log-output").textContent = state.logs.map((entry) =>
    `${formatTime(entry.time)} ${entry.level.padEnd(7)} [${entry.profile_id || "-"}] ${entry.message} ${JSON.stringify(entry.details || {})}`
  ).join("\n");
  $("log-output").scrollTop = $("log-output").scrollHeight;
}

function queueLogRender() {
  if (state.unloaded || state.logRenderId !== null) return;
  state.logRenderId = requestAnimationFrame(() => {
    state.logRenderId = null;
    renderLogs();
  });
}

async function connectSse() {
  if (state.unloaded || state.sseId || state.sseConnecting || activeTab() !== "logs") return;
  state.sseConnecting = true;
  const version = ++state.sseRevision;
  const current = () => !state.unloaded && version === state.sseRevision && activeTab() === "logs";
  try {
    const subscription = await bridge.subscribeSSE("logs/stream", {
      onOpen() { if (current()) $("sse-state").textContent = "SSE 已连接"; },
      onMessage(event) {
        if (!current()) return;
        const payload = event.parsed;
        if (payload?.type === "log" && payload.entry) {
          state.logs.push(payload.entry);
          state.logs = state.logs.slice(-400);
          queueLogRender();
        }
      },
      onError() {
        if (!current()) return;
        $("sse-state").textContent = "SSE 已断开，正在重连…";
        scheduleReconnect();
      },
    });
    if (!current()) {
      bridge.unsubscribeSSE(subscription);
    } else {
      state.sseId = subscription;
    }
  } catch {
    if (current()) {
      $("sse-state").textContent = "SSE 连接失败，正在重连…";
      scheduleReconnect();
    }
  } finally {
    state.sseConnecting = false;
    if (!state.unloaded && version !== state.sseRevision && activeTab() === "logs" && !state.sseId) scheduleReconnect();
  }
}

function scheduleReconnect() {
  state.sseRevision++;
  clearTimeout(state.reconnectTimer);
  const current = state.sseId;
  state.sseId = null;
  if (current) bridge.unsubscribeSSE(current);
  if (activeTab() === "logs" && !state.unloaded) {
    state.reconnectTimer = setTimeout(connectSse, 2500);
  }
}

function disconnectSse() {
  state.sseRevision++;
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  const current = state.sseId;
  state.sseId = null;
  if (current) bridge.unsubscribeSSE(current);
  if (!state.unloaded) $("sse-state").textContent = "打开运行日志时连接 SSE";
}

async function loadStorage() {
  const version = nextRequest("storage");
  const result = await bridge.apiGet("storage");
  if (!currentRequest("storage", version)) return;
  const cards = $("storage-cards");
  cards.replaceChildren(
    metric("数据库", bytes(result.database_size)),
    metric("日志总量", bytes(result.log_size)),
    metric("上次清理", formatTime(result.last_cleanup_at)),
    ...Object.entries(result.counts || {}).map(([key, value]) => metric(key, value)),
  );
}

function activeTab() {
  return document.querySelector(".tabs button.active")?.dataset.tab || "overview";
}

async function loadTab(tabName) {
  if (tabName === "overview") return Promise.all([loadStatus(), publishing.loadStatus()]);
  if (tabName === "publishing") return publishing.loadStatus();
  if (tabName === "post-reviews") return publishing.loadReviews("post");
  if (tabName === "comment-reviews") return publishing.loadReviews("comment");
  if (tabName === "login") return loadLogin();
  if (tabName === "settings") return undefined;
  if (tabName === "events") return Promise.all([loadEvents(), loadRiskUsers()]);
  if (tabName === "feed") return loadCandidates();
  if (tabName === "storage") return loadStorage();
  if (tabName === "logs") {
    await loadLogs();
    return connectSse();
  }
  return undefined;
}

async function initialLoad() {
  await bridge.ready();
  await Promise.all([loadConfig(), loadStatus(), publishing.loadStatus()]);
}

const publishing = createPublishing({ bridge, busy, confirmAction, metric, toast, formatTime, isUnloaded: () => state.unloaded });

document.querySelectorAll(".tabs button").forEach((button) => {
  button.addEventListener("click", async () => {
    if (state.unloaded) return;
    document.querySelectorAll(".tabs button").forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === button.dataset.tab));
    if (button.dataset.tab !== "logs") disconnectSse();
    try { await loadTab(button.dataset.tab); }
    catch (error) { toast(`${button.textContent}加载失败：${error.message}`, "error"); }
  });
});

$("refresh-all").addEventListener("click", () => busy($("refresh-all"), async () => {
  const tabName = activeTab();
  await Promise.all([
    loadStatus(),
    publishing.loadStatus(),
    tabName === "overview" ? Promise.resolve() : loadTab(tabName),
  ]);
  toast("状态已刷新");
}));
$("login-profile").addEventListener("change", () => {
  nextRequest("login");
  clearLogin();
  $("login-facts").textContent = "正在读取账号档案…";
  return loadLogin();
});
$("request-qr").addEventListener("click", () => loginAction("qr"));
$("check-login").addEventListener("click", () => loginAction("check"));
$("logout").addEventListener("click", () => loginAction("logout"));
$("save-config").addEventListener("click", () => busy($("save-config"), async () => {
  for (const control of $("config-form").querySelectorAll("input, select, textarea")) {
    if (!control.reportValidity()) return;
  }
  const result = await bridge.apiPost("config/save", state.config);
  const changed = (result.changed || []).join("、") || "无变化";
  $("config-result").textContent = `保存成功；已重载：${changed}`;
  toast(changed === "无变化" ? "设置已保存，配置内容无变化" : "设置已保存并生效", "success");
  await loadConfig();
  await Promise.all([loadStatus(), publishing.loadStatus()]);
}));
$("restore-defaults").addEventListener("click", () => busy($("restore-defaults"), async () => {
  const confirmed = await confirmAction(
    "确认恢复插件默认配置？登录凭证将完整保留。",
    { confirmText: "载入默认值" },
  );
  if (!confirmed) return;
  const defaults = await bridge.apiGet("config/defaults");
  state.config = defaults;
  renderConfigForm();
  $("config-result").textContent = "默认值已载入表单；点击“校验并保存”后生效。";
}));
$("search-events").addEventListener("click", () => busy($("search-events"), async () => {
  state.eventPage = 1;
  await loadEvents();
}));
for (const id of eventFilterIds) $(id).addEventListener("change", async () => {
  nextRequest("events");
  state.eventPage = 1;
  state.eventId = "";
  state.eventBack = null;
  $("event-back").hidden = true;
  if (id === "event-mode" && $(id).value === "uid" && !state.eventRangeInitialized) {
    state.eventRangeInitialized = true;
    if (!$("event-start").value && !$("event-end").value) {
      const now = new Date();
      const localInput = (date) => new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
      $("event-start").value = localInput(new Date(now.getTime() - 7 * 86400000));
      $("event-end").value = localInput(now);
    }
  }
  if (id === "event-profile") {
    nextRequest("risk-users");
    state.riskPage = 1;
    closeRiskDetail();
    $("risk-users").replaceChildren();
  }
  try { await Promise.all([loadEvents(), id === "event-profile" ? loadRiskUsers() : Promise.resolve()]); }
  catch (error) { toast(error.message, "error"); }
});
$("event-back").addEventListener("click", () => busy($("event-back"), async () => {
  if (!state.eventBack) return;
  const previous = state.eventBack;
  eventFilterIds.forEach((id, index) => { $(id).value = previous.values[index]; });
  state.eventPage = previous.page;
  state.eventId = "";
  state.eventBack = null;
  $("event-back").hidden = true;
  closeRiskDetail();
  await Promise.all([loadEvents(), loadRiskUsers()]);
}));
for (const id of ["risk-uid", "risk-level", "risk-manual"]) $(id).addEventListener("change", async () => {
  state.riskPage = 1;
  try { await loadRiskUsers(); } catch (error) { toast(error.message, "error"); }
});
$("search-risk").addEventListener("click", () => busy($("search-risk"), async () => {
  state.riskPage = 1;
  await loadRiskUsers();
}));
for (const [id, direction] of [["risk-prev", -1], ["risk-next", 1]]) {
  $(id).addEventListener("click", () => busy($(id), async () => {
    state.riskPage = Math.max(1, Math.min(state.riskPages, state.riskPage + direction));
    await loadRiskUsers();
  }));
}
$("close-risk").addEventListener("click", closeRiskDetail);
for (const [id, direction] of [["refresh-risk", 0], ["risk-audit-prev", -1], ["risk-audit-next", 1]]) {
  $(id).addEventListener("click", () => busy($(id), async () => {
    if (!state.riskSelected || state.riskActionBusy) return;
    const { profile_id, uid } = state.riskSelected;
    const page = Math.max(1, Math.min(state.riskAuditPages, state.riskAuditPage + direction));
    await loadRiskDetail(profile_id, uid, page);
  }));
}
for (const action of ["block", "unblock", "adjust"]) {
  $("risk-" + action).addEventListener("click", async () => {
    try { await bridge.ready(); await changeRisk(action); }
    catch (error) { toast(error.message, "error"); }
  });
}
$("event-prev").addEventListener("click", () => busy($("event-prev"), async () => {
  state.eventPage = Math.max(1, state.eventPage - 1);
  await loadEvents();
}));
$("event-next").addEventListener("click", () => busy($("event-next"), async () => {
  state.eventPage = Math.min(state.eventPages, state.eventPage + 1);
  await loadEvents();
}));
$("search-logs").addEventListener("click", () => busy($("search-logs"), loadLogs));
$("reject-expired").addEventListener("click", () => busy($("reject-expired"), async () => {
  const confirmed = await confirmAction(
    "确认拒绝 72 小时前全部待审核候选？",
    { confirmText: "批量拒绝", danger: true },
  );
  if (!confirmed) return;
  const result = await bridge.apiPost("feed/candidates/reject-expired", { older_than_hours: 72 });
  toast(`已拒绝 ${result.rejected} 条`);
  await loadCandidates();
}));
$("copy-diagnostics").addEventListener("click", () => busy($("copy-diagnostics"), async () => {
  const result = await bridge.apiGet("diagnostics");
  const diagnosticText = JSON.stringify(result, null, 2);
  const fallback = $("diagnostics-fallback");
  const output = $("diagnostics-output");
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
    await navigator.clipboard.writeText(diagnosticText);
    fallback.hidden = true;
    toast("脱敏诊断已复制");
  } catch {
    output.value = diagnosticText;
    fallback.hidden = false;
    output.focus();
    output.select();
    let copied = false;
    try { copied = document.execCommand("copy"); }
    catch { copied = false; }
    toast(copied ? "脱敏诊断已复制" : "剪贴板权限受限，请在下方文本框中手动复制");
  }
}));
$("preview-cleanup").addEventListener("click", () => busy($("preview-cleanup"), async () => {
  $("cleanup-output").textContent = JSON.stringify(await bridge.apiGet("storage/cleanup-preview"), null, 2);
}));
$("run-cleanup").addEventListener("click", () => busy($("run-cleanup"), async () => {
  const confirmed = await confirmAction(
    "仅清理插件自身的到期数据。确认执行？",
    { confirmText: "执行清理", danger: true },
  );
  if (!confirmed) return;
  $("cleanup-output").textContent = JSON.stringify(await bridge.apiPost("storage/cleanup", { confirm: true }), null, 2);
  await loadStorage();
}));

$("confirm-cancel").addEventListener("click", () => closeConfirmation(false));
$("confirm-accept").addEventListener("click", () => closeConfirmation(true));
$("confirm-overlay").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeConfirmation(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.confirmResolve) closeConfirmation(false);
  if (event.key === "Tab" && state.confirmResolve) {
    event.preventDefault();
    (document.activeElement === $("confirm-accept") ? $("confirm-cancel") : $("confirm-accept")).focus();
  }
});

function dispose() {
  if (state.unloaded) return;
  state.unloaded = true;
  if (state.confirmResolve) closeConfirmation(false);
  clearInterval(state.loginTimer);
  clearTimeout(state.loginRefreshTimer);
  clearTimeout(state.reconnectTimer);
  clearTimeout(state.toastTimer);
  if (state.logRenderId !== null) cancelAnimationFrame(state.logRenderId);
  disconnectSse();
}
window.addEventListener("pagehide", dispose);
window.addEventListener("beforeunload", dispose);

initialLoad().catch((error) => {
  if (state.unloaded) return;
  $("hero-summary").textContent = `管理页加载失败：${error.message}`;
  toast(error.message);
});
