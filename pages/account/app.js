const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const labels = {
  idle: "尚未开始", requesting: "正在获取二维码…", wait: "等待扫码",
  scanned: "已扫码，等待服务端确认结果；若手机已确认，请等待此处更新",
  saving: "已收到登录确认，正在保存账号…", success: "登录成功，账号已保存",
  expired: "二维码已过期，服务端未返回可保存的登录结果，请获取新码重试", canceled: "扫码已取消",
  failed: "登录失败，原账号保留。请检查网络或重新获取二维码",
};
let timer;
let login = { state: "idle" };
let account = {};
let pending = "";
let polling = false;
let revision = 0;
let stopped = false;
let connected = false;
let qrStale = false;
const active = () => ["requesting", "wait", "scanned", "saving"].includes(login.state);
const operations = {
  load: "正在连接并读取账号…", start: "正在获取新二维码，请稍候…",
  cancel: "正在取消扫码…", check: "正在检查当前账号的登录状态…", logout: "正在清除本地登录态…",
};

function renderSteps(state) {
  const current = { idle: 0, requesting: 0, wait: 0, scanned: 1, saving: 2 }[state] ?? -1;
  const completed = state === "success" ? 3 : Math.max(0, current);
  for (let index = 0; index < 3; index++) {
    const id = `step-${index + 1}`;
    const done = index < completed;
    const selected = index === current;
    $(id).dataset.step = done ? "done" : selected ? "current" : "pending";
    $(id + "-number").textContent = done ? "✓" : String(index + 1);
    $(id + "-state").textContent = done ? "已完成" : selected ? "进行中" : "未开始";
    if (selected) $(id).setAttribute("aria-current", "step");
    else $(id).removeAttribute("aria-current");
  }
}

function controls() {
  for (const id of ["start", "cancel", "check", "logout"]) {
    $(id).disabled = !connected || Boolean(pending) ||
      (id === "cancel" && !active()) || (id === "logout" && !account.has_credentials);
  }
  $("start").textContent = pending === "start" ? "正在获取…" :
    login.state === "idle" && !qrStale ? "获取二维码" : "重新获取二维码";
  $("check").textContent = pending === "check" ? "正在检查…" : "检查登录态";
  $("cancel").textContent = pending === "cancel" ? "正在取消…" : "取消扫码";
  $("logout").textContent = pending === "logout" ? "正在清除…" : "清除本地登录态";
}

function renderQr() {
  const remaining = Math.max(0, Math.ceil((login.expires_at || 0) - Date.now() / 1000));
  const elapsed = Boolean(login.expires_at) && remaining === 0;
  renderSteps(pending === "start" ? "requesting" : qrStale ? "unknown" :
    elapsed && ["wait", "scanned"].includes(login.state) ? "expired" : login.state);
  const showQr = Boolean(login.qr_image) && ["wait", "scanned"].includes(login.state) &&
    !elapsed && !qrStale && pending !== "start";
  $("qr").hidden = !showQr;
  if (showQr) $("qr").src = login.qr_image;
  else $("qr").removeAttribute("src");
  $("qr-placeholder").hidden = showQr;
  $("qr-stage").setAttribute("aria-busy", String(pending === "start" || login.state === "requesting"));
  const placeholders = {
    idle: ["准备好小黑盒 App", "点击下方按钮获取登录二维码"],
    requesting: ["正在获取二维码…", "请稍候，二维码将在这里显示"],
    saving: ["正在保存账号…", "已收到服务端确认，请等待保存完成"],
    success: ["账号已保存", "现在可以使用当前账号发帖"],
    expired: ["二维码已过期", "点击「重新获取二维码」后再次扫码"],
    canceled: ["扫码已取消", "需要登录时可重新获取二维码"],
    failed: ["本次登录未完成", "查看下方提示，或重新获取二维码"],
  };
  const placeholder = pending === "start" ? placeholders.requesting :
    qrStale ? ["二维码获取结果未确认", "请重新获取，避免扫描旧二维码"] :
    elapsed && ["wait", "scanned"].includes(login.state) ? placeholders.expired :
    placeholders[login.state] || ["等待二维码", "请获取新的二维码"];
  $("qr-placeholder-title").textContent = placeholder[0];
  $("qr-placeholder-hint").textContent = placeholder[1];
  $("expiry").textContent = showQr ? `有效期剩余 ${remaining} 秒` : "";
  $("status").textContent = qrStale ? pending === "start" ? labels.requesting : "新二维码结果未确认，请重新获取。" :
    elapsed && ["wait", "scanned"].includes(login.state) ?
    "二维码有效期已结束，等待服务端最终结果；也可重新获取二维码。" :
    (login.message || labels[login.state] || "未知状态") +
      (login.retrying ? `。查询暂时失败，正在重试（连续 ${login.retries} 次）` : "");
}

function render(data) {
  if (typeof data?.account?.has_credentials !== "boolean" || typeof data?.login?.state !== "string") {
    throw new Error("Invalid account snapshot");
  }
  account = data.account;
  login = data.login;
  qrStale = false;
  $("account-state").textContent = !account.has_credentials ? "未登录" :
    account.check_unavailable ? "暂时无法验证，请稍后重试" :
    account.logged_in === true ? "已登录" : account.logged_in === false ? "登录已失效" : "已配置，待检查";
  $("account").textContent = !account.has_credentials ? "尚未配置账号" :
    `${account.nickname || "已配置账号"} · ID ${account.user_id || "未填写"}`;
  $("account-state").dataset.tone = account.has_credentials && account.logged_in === true ? "success" :
    account.check_unavailable || account.has_credentials && account.logged_in === false ? "warning" : "neutral";
  $("account-hint").textContent = !account.has_credentials ? "扫码登录后可检查账号状态。" :
    account.check_unavailable ? "暂时无法连接服务端，现有凭据仍保留。" :
    account.logged_in === false ? "请重新扫码登录，更新已失效的凭据。" :
    account.logged_in === true ? "当前账号可用于本插件发帖。" : "点击「检查登录态」验证当前凭据。";
  $("progress").textContent = login.session_id ? `本次会话 ${login.session_id} · 已查询 ${login.poll_count || 0} 次` +
    (login.updated_at ? ` · 最后响应 ${new Date(login.updated_at * 1000).toLocaleTimeString()}` : "") : "";
  $("diagnostics").hidden = !login.session_id;
  renderQr();
  controls();
}

async function run(action, operation) {
  if (pending || stopped || !connected && action !== "load") return;
  revision++;
  pending = action;
  if (action === "start") qrStale = true;
  controls();
  renderQr();
  $("error").textContent = "";
  $("operation").textContent = operations[action];
  try {
    const data = await operation();
    if (stopped) return;
    render(data);
    $("operation").textContent = action === "check" ? "登录态已更新。" :
      action === "logout" ? "本地凭据与备份已清除。" : "";
  }
  catch {
    if (!stopped) {
      $("operation").textContent = "";
      $("error").textContent = connected ? "操作失败，请检查 Dashboard 登录状态、网络和插件日志后重试。" :
        "无法连接 AstrBot WebUI，请确认 Dashboard 登录状态后刷新页面重试。";
    }
  }
  finally {
    pending = "";
    if (!stopped) { controls(); renderQr(); }
  }
}

async function poll() {
  if (stopped) return;
  renderQr();
  if (!active() || pending || polling) return;
  polling = true;
  const version = revision;
  try {
    const data = await bridge.apiGet("auth/status");
    if (!stopped && version === revision) {
      render(data);
      $("error").textContent = "";
    }
  } catch {
    if (!stopped && version === revision) {
      $("error").textContent = "暂时无法更新扫码状态，正在重试。请检查网络；也可取消或重新获取二维码。";
    }
  } finally { polling = false; }
}

$("start").addEventListener("click", () => run("start", () => bridge.apiPost("auth/qr", {})));
$("cancel").addEventListener("click", () => run("cancel", () => bridge.apiPost("auth/cancel", {})));
$("check").addEventListener("click", () => run("check", () => bridge.apiPost("auth/check", {})));
$("logout").addEventListener("click", () => {
  if (pending || stopped || !account.has_credentials) return;
  $("logout-confirm").returnValue = "cancel";
  $("logout-confirm").showModal();
});
$("logout-confirm").addEventListener("close", () => {
  if ($("logout-confirm").returnValue === "logout") {
    run("logout", () => bridge.apiPost("auth/logout", { confirm: true }));
  }
});

window.addEventListener("pagehide", () => {
  stopped = true;
  revision++;
  clearInterval(timer);
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});
controls();
if (!bridge) {
  $("error").textContent = "请从 AstrBot WebUI 的插件可视化视图打开本页面。";
} else {
  await run("load", async () => {
    await bridge.ready();
    if (stopped) return;
    connected = true;
    return bridge.apiGet("auth/status");
  });
  if (!stopped && account.has_credentials && !active()) await run("check", () => bridge.apiPost("auth/check", {}));
  if (!stopped && connected) timer = setInterval(poll, 2000);
}
