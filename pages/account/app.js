const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const labels = {
  idle: "尚未开始", requesting: "正在获取二维码…", wait: "等待扫码",
  scanned: "已扫码，请在小黑盒 App 中确认", success: "登录成功，账号已保存",
  expired: "二维码已过期，请重新获取", canceled: "扫码已取消",
  failed: "登录失败，原账号保留。请检查网络或重新获取二维码",
};
let timer;
let expiresAt = 0;
let busy = false;
let active = false;

function render(data) {
  const account = data.account;
  $("account").textContent = !account.has_credentials ? "尚未配置账号" :
    `${account.nickname || "已配置账号"} · ID ${account.user_id || "未填写"} · ${
      account.logged_in === true ? "登录有效" : account.logged_in === false ? "登录失效或无法验证" : "待检查"}`;
  const login = data.login;
  $("status").textContent = labels[login.state] || "未知状态";
  active = ["requesting", "wait", "scanned"].includes(login.state);
  expiresAt = login.expires_at || 0;
  $("qr").hidden = !login.qr_image;
  if (login.qr_image) $("qr").src = login.qr_image;
  else $("qr").removeAttribute("src");
  $("cancel").disabled = !active;
}

async function run(operation) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  $("error").textContent = "";
  try { render(await operation()); }
  catch { $("error").textContent = "操作失败，请检查 Dashboard 登录状态、网络和插件日志后重试。"; }
  finally {
    busy = false;
    document.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    $("cancel").disabled = !active;
  }
}

$("start").addEventListener("click", () => run(() => bridge.apiPost("auth/qr", {})));
$("cancel").addEventListener("click", () => run(() => bridge.apiPost("auth/cancel", {})));
$("check").addEventListener("click", () => run(() => bridge.apiPost("auth/check", {})));
$("logout").addEventListener("click", () => {
  $("logout-confirm").returnValue = "cancel";
  $("logout-confirm").showModal();
});
$("logout-confirm").addEventListener("close", () => {
  if ($("logout-confirm").returnValue === "logout") {
    run(() => bridge.apiPost("auth/logout", {}));
  }
});

if (!bridge) {
  $("error").textContent = "请从 AstrBot WebUI 的插件可视化视图打开本页面。";
  document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
} else {
  await run(async () => { await bridge.ready(); return bridge.apiGet("auth/status"); });
  timer = setInterval(() => {
    $("expiry").textContent = expiresAt ? `剩余 ${Math.max(0, Math.ceil(expiresAt - Date.now() / 1000))} 秒` : "";
    if (active && !busy) run(() => bridge.apiGet("auth/status"));
  }, 2000);
}
window.addEventListener("pagehide", () => clearInterval(timer));
