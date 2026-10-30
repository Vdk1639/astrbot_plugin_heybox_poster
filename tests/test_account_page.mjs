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

const { elements, state, trigger } = await page();
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
state.data.login = { state: "scanned", qr_image: "data:image/png;base64,test",
  session_id: "abc123", poll_count: 4, updated_at: Date.now() / 1000, retrying: true, retries: 2 };
state.timers[0]();
await new Promise(setImmediate);
assert.match(elements.get("status").textContent, /已扫码/);
assert.match(elements.get("status").textContent, /等待服务端确认/);
assert.match(elements.get("status").textContent, /正在重试/);
assert.match(elements.get("progress").textContent, /abc123.*4.*最后响应/);
assert.equal(elements.get("diagnostics").hidden, false);
state.data.login.retrying = false;
state.timers[0]();
await new Promise(setImmediate);
assert.doesNotMatch(elements.get("status").textContent, /正在重试/);
state.data = { account: { has_credentials: true, logged_in: true, user_id: "99", nickname: "<script>test</script>" },
  login: { state: "success" } };
await trigger("check");
assert.equal(elements.get("qr").hidden, true);
assert.equal(elements.get("qr").src, undefined);
assert.match(elements.get("account").textContent, /99/);
assert.match(elements.get("account-state").textContent, /已登录/);
assert.equal(elements.get("cancel").disabled, true);
assert.equal(elements.get("qr-placeholder-title").textContent, "账号已保存");
assert.equal(elements.get("diagnostics").hidden, true);
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

state.fail = true;
await trigger("start");
assert.match(elements.get("error").textContent, /操作失败/);
assert.doesNotMatch(elements.get("error").textContent, /secret-payload/);
assert.equal(elements.get("start").disabled, false);
assert.match(elements.get("qr-placeholder-title").textContent, /未确认/);
assert.doesNotMatch(elements.get("status").textContent, /登录成功/);
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
assert.equal(expiry.elements.get("start").disabled, false);
const qrRequests = expiry.state.calls.filter(([, path]) => path === "auth/qr").length;
await expiry.state.timers[0]();
assert.equal(expiry.state.calls.filter(([, path]) => path === "auth/qr").length, qrRequests);
expiry.state.data.login = { state: "expired", qr_image: "data:image/png;base64,old" };
await expiry.state.timers[0]();
assert.equal(expiry.elements.get("qr").hidden, true);
assert.equal(expiry.elements.get("cancel").disabled, true);

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

// 前台请求有具体反馈且去重；刷新期间不再展示旧码。
let finishPost;
race.state.post = () => new Promise((resolve) => { finishPost = resolve; });
const newQr = race.trigger("start");
assert.equal(race.elements.get("start").textContent, "正在获取…");
assert.match(race.elements.get("operation").textContent, /正在获取新二维码/);
assert.equal(race.elements.get("qr-stage")["aria-busy"], "true");
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
console.log("Account page bridge, QR expiry, concurrent operations, redaction, confirmation and lifecycle checks passed.");
