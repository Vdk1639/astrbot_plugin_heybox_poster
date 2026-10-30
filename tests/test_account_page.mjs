// 在 AstrBot bridge 和 DOM 桩上执行实际页面脚本，不调用真实账号。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../pages/account/app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../pages/account/index.html", import.meta.url), "utf8");

async function page(withBridge = true, setup = () => {}) {
  const elements = new Map();
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) {
    elements.set(id, {
      textContent: "", hidden: false, disabled: false, events: {}, dataset: {},
      style: { writes: 0, setProperty(name, value) { this[name] = value; this.writes++; } },
      addEventListener(name, handler) { this.events[name] = handler; },
      setAttribute(name, value) { this[name] = value; },
      removeAttribute(name) { delete this[name]; },
      showModal() { this.open = true; },
    });
  }
  const buttons = ["check", "logout", "start", "cancel"].map((id) => elements.get(id));
  const initial = { account: { has_credentials: true, user_id: "42" }, login: { state: "idle" } };
  const state = { data: initial, calls: [], bodies: [], timers: [], events: {}, fail: false };
  setup(state);
  const bridge = {
    ready: async () => { await state.ready; },
    apiGet: async (path) => {
      state.calls.push(["GET", path]);
      if (state.fail) throw new Error("secret-payload");
      if (state.get) return state.get();
      return state.data;
    },
    apiPost: async (path, body) => {
      state.calls.push(["POST", path]);
      state.bodies.push(body);
      if (state.fail) throw new Error("secret-payload");
      if (state.post) return state.post();
      return state.data;
    },
  };
  const sandbox = {
    window: { AstrBotPluginPage: withBridge ? bridge : undefined,
      addEventListener: (name, handler) => { state.events[name] = handler; },
      location: { reload: () => { state.reloaded = true; } } },
    document: { getElementById: (id) => elements.get(id), querySelectorAll: () => buttons },
    setInterval: (callback) => { state.timers.push(callback); return 1; },
    clearInterval: () => { state.cleared = true; },
  };
  await vm.runInNewContext(`(async () => { ${source}\n })()`, sandbox);
  const trigger = async (id, event = "click") => {
    await elements.get(id).events[event]();
    await new Promise(setImmediate);
  };
  return { elements, state, trigger };
}

function assertSteps(elements, expected, focus = expected.indexOf("current")) {
  assert.equal(elements.get("login-steps").dataset.focus, focus < 0 ? "none" : String(focus));
  if (focus >= 0) assert.equal(elements.get("login-steps").style["--step-index"], String(focus));
  for (const [index, status] of expected.entries()) {
    const id = `step-${index + 1}`;
    assert.equal(elements.get(id).dataset.step, status, id);
    assert.equal(elements.get(id)["aria-current"], status === "current" ? "step" : undefined, id);
    assert.equal(elements.get(id + "-number").textContent, status === "done" ? "✓" : String(index + 1), id);
    assert.equal(elements.get(id + "-state").textContent,
      status === "done" ? "已完成" : status === "current" ? "进行中" : "未开始", id);
  }
}

const { elements, state, trigger } = await page();
assertSteps(elements, ["current", "pending", "pending"]);
assert.match(elements.get("account").textContent, /42/);
assert.deepEqual(state.calls.slice(0, 2), [["GET", "auth/status"], ["POST", "auth/check"]]);
assert.match(elements.get("account-state").textContent, /待检查/);
state.data = { account: { has_credentials: true, user_id: "42" },
  login: { state: "wait", qr_image: "data:image/png;base64,test", expires_at: Date.now() / 1000 + 120 } };
await trigger("start");
assert.deepEqual(state.calls.at(-1), ["POST", "auth/qr"]);
assert.equal(elements.get("qr").hidden, false);
assert.equal(elements.get("cancel").disabled, false);
assert.equal(elements.get("qr-placeholder").hidden, true);
assert.match(elements.get("expiry").textContent, /有效期剩余/);
assertSteps(elements, ["current", "pending", "pending"]);
state.data.login = { state: "scanned", qr_image: "data:image/png;base64,test",
  session_id: "abc123", poll_count: 4, updated_at: Date.now() / 1000, retrying: true, retries: 2 };
state.timers[0]();
await new Promise(setImmediate);
assert.match(elements.get("status").textContent, /已扫码/);
assert.match(elements.get("status").textContent, /等待服务端确认/);
assert.match(elements.get("status").textContent, /正在重试/);
assert.match(elements.get("progress").textContent, /abc123.*4.*最后响应/);
assert.equal(elements.get("diagnostics").hidden, false);
assertSteps(elements, ["done", "current", "pending"]);
const focusWrites = elements.get("login-steps").style.writes;
state.data.login.retrying = false;
state.timers[0]();
await new Promise(setImmediate);
assert.doesNotMatch(elements.get("status").textContent, /正在重试/);
assertSteps(elements, ["done", "current", "pending"]);
assert.equal(elements.get("login-steps").style.writes, focusWrites);
state.data.login = { state: "saving" };
await state.timers[0]();
assertSteps(elements, ["done", "done", "current"]);
const savingFocusWrites = elements.get("login-steps").style.writes;
state.data = { account: { has_credentials: true, logged_in: true, user_id: "99", nickname: "<script>test</script>" },
  login: { state: "success" } };
await state.timers[0]();
assert.equal(elements.get("qr").hidden, true);
assert.equal(elements.get("qr").src, undefined);
assert.match(elements.get("account").textContent, /99/);
assert.match(elements.get("account-state").textContent, /已登录/);
assert.equal(elements.get("cancel").disabled, true);
assert.equal(elements.get("qr-placeholder-title").textContent, "账号已保存");
assert.equal(elements.get("diagnostics").hidden, true);
assertSteps(elements, ["done", "done", "done"], 2);
assert.equal(elements.get("login-steps").style.writes, savingFocusWrites);
assert.equal(elements.get("login-steps").style["--step-index"], "2");
state.data.account = { has_credentials: true, check_unavailable: true, logged_in: null, user_id: "99" };
await trigger("check");
assert.match(elements.get("account-state").textContent, /暂时无法验证/);
state.data.account = { has_credentials: true, logged_in: false };
await trigger("check");
assert.match(elements.get("account-state").textContent, /登录已失效/);

const count = state.calls.length;
await trigger("logout");
assert.equal(elements.get("logout-confirm").open, true);
assert.equal(state.calls.length, count);
await trigger("logout-confirm", "close");
assert.equal(state.calls.length, count);
elements.get("logout-confirm").returnValue = "logout";
state.data = { account: { has_credentials: false }, login: { state: "idle" } };
await trigger("logout-confirm", "close");
assert.deepEqual(state.calls.at(-1), ["POST", "auth/logout"]);
assert.equal(state.bodies.at(-1).confirm, true);
assert.match(elements.get("account").textContent, /尚未配置/);
assert.equal(elements.get("account-state").textContent, "未登录");
assert.equal(elements.get("logout").disabled, true);
assert.match(elements.get("operation").textContent, /本地凭据与备份已清除/);

state.data.login = { state: "failed", message: "读取新账号 ID 失败，原账号保留。" };
await trigger("check");
assert.match(elements.get("status").textContent, /读取新账号 ID/);
assertSteps(elements, ["pending", "pending", "pending"]);

state.fail = true;
await trigger("start");
assert.match(elements.get("error").textContent, /操作失败/);
assert.doesNotMatch(elements.get("error").textContent, /secret-payload/);
assert.equal(elements.get("start").disabled, false);
assert.match(elements.get("qr-placeholder-title").textContent, /未确认/);
assert.doesNotMatch(elements.get("status").textContent, /登录成功/);
assertSteps(elements, ["pending", "pending", "pending"]);
state.events.pagehide();
assert.equal(state.cleared, true);
state.events.pageshow({ persisted: true });
assert.equal(state.reloaded, true);

// 本地计时结束只隐藏旧码，仍等待服务端确认；绝不自动创建新会话。
const expiry = await page();
expiry.state.data = { account: { has_credentials: true, user_id: "42" },
  login: { state: "wait", qr_image: "data:image/png;base64,old", expires_at: Date.now() / 1000 - 1 } };
await expiry.trigger("start");
assert.equal(expiry.elements.get("qr").hidden, true);
assert.equal(expiry.elements.get("qr").src, undefined);
assert.match(expiry.elements.get("qr-placeholder-title").textContent, /过期/);
assert.match(expiry.elements.get("status").textContent, /等待服务端最终结果/);
assertSteps(expiry.elements, ["pending", "pending", "pending"]);
assert.equal(expiry.elements.get("start").disabled, false);
const qrRequests = expiry.state.calls.filter(([, path]) => path === "auth/qr").length;
await expiry.state.timers[0]();
assert.equal(expiry.state.calls.filter(([, path]) => path === "auth/qr").length, qrRequests);
expiry.state.data.login = { state: "expired", qr_image: "data:image/png;base64,old" };
await expiry.state.timers[0]();
assert.equal(expiry.elements.get("qr").hidden, true);
assert.equal(expiry.elements.get("cancel").disabled, true);
assertSteps(expiry.elements, ["pending", "pending", "pending"]);

// 后台请求不阻止取消，延迟到达的旧快照不能覆盖取消结果。
const race = await page();
const waiting = { account: { has_credentials: true, user_id: "42" },
  login: { state: "wait", qr_image: "data:image/png;base64,test", expires_at: Date.now() / 1000 + 120 } };
race.state.data = waiting;
await race.trigger("start");
let finishPoll;
race.state.get = () => new Promise((resolve) => { finishPoll = resolve; });
const oldPoll = race.state.timers[0]();
assert.equal(race.elements.get("cancel").disabled, false);
assert.equal(race.elements.get("start").disabled, false);
const pollingCalls = race.state.calls.length;
await race.state.timers[0]();
assert.equal(race.state.calls.length, pollingCalls);
race.state.data = { account: waiting.account, login: { state: "canceled" } };
await race.trigger("cancel");
finishPoll(waiting);
await oldPoll;
assert.equal(race.elements.get("status").textContent, "扫码已取消");
assert.equal(race.elements.get("qr").hidden, true);
assertSteps(race.elements, ["pending", "pending", "pending"]);

// 前台请求有具体反馈且去重；刷新期间不再展示旧码。
let finishPost;
race.state.post = () => new Promise((resolve) => { finishPost = resolve; });
const newQr = race.trigger("start");
assert.equal(race.elements.get("start").textContent, "正在获取…");
assert.match(race.elements.get("operation").textContent, /正在获取新二维码/);
assert.equal(race.elements.get("qr-stage")["aria-busy"], "true");
assertSteps(race.elements, ["current", "pending", "pending"]);
const foregroundCalls = race.state.calls.length;
await race.trigger("start");
assert.equal(race.state.calls.length, foregroundCalls);
finishPost(waiting);
await newQr;
assert.equal(race.elements.get("start").disabled, false);
assert.equal(race.elements.get("qr-stage")["aria-busy"], "false");
assert.equal(race.elements.get("qr").hidden, false);

// 轮询断网保留可操作性，恢复后自动清除错误且不暴露异常响应。
race.state.get = undefined;
race.state.fail = true;
await race.state.timers[0]();
assert.match(race.elements.get("error").textContent, /正在重试/);
assert.doesNotMatch(race.elements.get("error").textContent, /secret-payload/);
assert.equal(race.elements.get("cancel").disabled, false);
race.state.fail = false;
race.state.data = waiting;
await race.state.timers[0]();
assert.equal(race.elements.get("error").textContent, "");

// 页面离开后，未完成的请求既不能写回成功，也不能启动额外轮询。
race.state.get = () => new Promise((resolve) => { finishPoll = resolve; });
const departingPoll = race.state.timers[0]();
race.state.events.pagehide();
finishPoll({ account: { has_credentials: true, user_id: "99" }, login: { state: "success" } });
await departingPoll;
assert.doesNotMatch(race.elements.get("account").textContent, /99/);
const departingCalls = race.state.calls.length;
await race.state.timers[0]();
assert.equal(race.state.calls.length, departingCalls);

let finishReady;
let initializingState;
const initializing = page(true, (next) => {
  initializingState = next;
  next.ready = new Promise((resolve) => { finishReady = resolve; });
});
initializingState.events.pagehide();
finishReady();
await initializing;
assert.equal(initializingState.calls.length, 0);
assert.equal(initializingState.timers.length, 0);

const malformed = await page();
for (const invalid of [null, {}, { account: null, login: null }, { account: {}, login: { state: "wait" } }]) {
  malformed.state.data = invalid;
  await malformed.trigger("start");
  assert.match(malformed.elements.get("error").textContent, /操作失败/);
  assert.equal(malformed.elements.get("start").disabled, false);
  assert.match(malformed.elements.get("account").textContent, /42/);
}
malformed.state.data = waiting;
await malformed.trigger("start");
assert.equal(malformed.elements.get("qr").hidden, false);
assert.equal(malformed.elements.get("error").textContent, "");
malformed.state.data = null;
await malformed.state.timers[0]();
assert.match(malformed.elements.get("error").textContent, /正在重试/);
assert.equal(malformed.elements.get("cancel").disabled, false);

// 刷新立即重置旧成功；刷新失败、未知状态和到期均不能复活完成标记。
const progress = await page(true, (next) => {
  next.data = { account: { has_credentials: true }, login: { state: "success" } };
});
assertSteps(progress.elements, ["done", "done", "done"], 2);
let rejectRefresh;
progress.state.post = () => new Promise((resolve, reject) => { rejectRefresh = reject; });
const refreshing = progress.trigger("start");
assertSteps(progress.elements, ["current", "pending", "pending"]);
rejectRefresh(new Error("secret-payload"));
await refreshing;
assertSteps(progress.elements, ["pending", "pending", "pending"]);
assert.match(progress.elements.get("qr-placeholder-title").textContent, /未确认/);
progress.state.post = undefined;
progress.state.data.login = { state: "scanned", expires_at: Date.now() / 1000 + 120 };
await progress.trigger("start");
assertSteps(progress.elements, ["done", "current", "pending"]);
const retryFocusWrites = progress.elements.get("login-steps").style.writes;
progress.state.fail = true;
await progress.state.timers[0]();
assertSteps(progress.elements, ["done", "current", "pending"]);
assert.equal(progress.elements.get("login-steps").style.writes, retryFocusWrites);
progress.state.fail = false;
progress.state.data.login.retrying = true;
progress.state.data.login.retries = 1;
await progress.state.timers[0]();
assertSteps(progress.elements, ["done", "current", "pending"]);
assert.equal(progress.elements.get("login-steps").style.writes, retryFocusWrites);
progress.state.data.login.expires_at = Date.now() / 1000 - 1;
await progress.state.timers[0]();
assertSteps(progress.elements, ["pending", "pending", "pending"]);
assert.match(progress.elements.get("qr-placeholder-title").textContent, /过期/);
for (const loginState of ["canceled", "failed", "expired", "unexpected"]) {
  progress.state.data.login = { state: loginState };
  await progress.trigger("check");
  assertSteps(progress.elements, ["pending", "pending", "pending"]);
}
progress.state.data.login = { state: "requesting" };
await progress.trigger("check");
assertSteps(progress.elements, ["current", "pending", "pending"]);
progress.state.data.login = { state: "success" };
await progress.state.timers[0]();
assertSteps(progress.elements, ["done", "done", "done"], 2);

// 第二步刷新时底板回到第一步，结果未确认也不会复用旧焦点。
progress.state.data.login = { state: "scanned" };
await progress.trigger("check");
assert.equal(progress.elements.get("login-steps").style["--step-index"], "1");
progress.state.post = () => new Promise((resolve, reject) => { rejectRefresh = reject; });
const resetting = progress.trigger("start");
assertSteps(progress.elements, ["current", "pending", "pending"]);
assert.equal(progress.elements.get("login-steps").style["--step-index"], "0");
rejectRefresh(new Error("secret-payload"));
await resetting;
assertSteps(progress.elements, ["pending", "pending", "pending"]);
assert.equal(progress.elements.get("login-steps").style["--step-index"], "0");

const disconnected = await page(true, (next) => {
  next.ready = Promise.reject(new Error("secret-payload"));
});
assert.match(disconnected.elements.get("error").textContent, /刷新页面重试/);
assert.doesNotMatch(disconnected.elements.get("error").textContent, /secret-payload/);
assert.equal(disconnected.elements.get("start").disabled, true);
assert.equal(disconnected.state.calls.length, 0);

const standalone = await page(false);
assert.match(standalone.elements.get("error").textContent, /AstrBot WebUI/);
assert.equal(standalone.elements.get("start").disabled, true);
console.log("Account page bridge, login steps, QR expiry, concurrent operations, redaction, confirmation and lifecycle checks passed.");
