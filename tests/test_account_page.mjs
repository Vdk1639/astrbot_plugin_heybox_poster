// 在 AstrBot bridge 和 DOM 桩上执行实际页面脚本，不调用真实账号。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../pages/account/app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../pages/account/index.html", import.meta.url), "utf8");

async function page(withBridge = true) {
  const elements = new Map();
  for (const [, id] of html.matchAll(/id="([^"]+)"/g)) {
    elements.set(id, {
      textContent: "", hidden: false, disabled: false, events: {},
      addEventListener(name, handler) { this.events[name] = handler; },
      removeAttribute(name) { delete this[name]; },
      showModal() { this.open = true; },
    });
  }
  const buttons = ["check", "logout", "start", "cancel"].map((id) => elements.get(id));
  const initial = { account: { has_credentials: true, user_id: "42" }, login: { state: "idle" } };
  const state = { data: initial, calls: [], timers: [], events: {}, fail: false };
  const bridge = {
    ready: async () => ({}),
    apiGet: async (path) => {
      state.calls.push(["GET", path]);
      if (state.fail) throw new Error("secret-payload");
      return state.data;
    },
    apiPost: async (path) => {
      state.calls.push(["POST", path]);
      if (state.fail) throw new Error("secret-payload");
      return state.data;
    },
  };
  const sandbox = {
    window: { AstrBotPluginPage: withBridge ? bridge : undefined,
      addEventListener: (name, handler) => { state.events[name] = handler; } },
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
state.data = { account: { has_credentials: true, user_id: "42" },
  login: { state: "wait", qr_image: "data:image/png;base64,test", expires_at: Date.now() / 1000 + 120 } };
await trigger("start");
assert.deepEqual(state.calls.at(-1), ["POST", "auth/qr"]);
assert.equal(elements.get("qr").hidden, false);
assert.equal(elements.get("cancel").disabled, false);
state.data.login = { state: "scanned", qr_image: "data:image/png;base64,test" };
state.timers[0]();
await new Promise(setImmediate);
assert.match(elements.get("status").textContent, /已扫码/);
state.data = { account: { has_credentials: true, logged_in: true, user_id: "99", nickname: "<script>test</script>" },
  login: { state: "success" } };
await trigger("check");
assert.equal(elements.get("qr").hidden, true);
assert.equal(elements.get("qr").src, undefined);
assert.match(elements.get("account").textContent, /99/);
assert.equal(elements.get("cancel").disabled, true);

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
assert.match(elements.get("account").textContent, /尚未配置/);

state.fail = true;
await trigger("start");
assert.match(elements.get("error").textContent, /操作失败/);
assert.doesNotMatch(elements.get("error").textContent, /secret-payload/);
assert.equal(elements.get("start").disabled, false);
state.events.pagehide();
assert.equal(state.cleared, true);

const standalone = await page(false);
assert.match(standalone.elements.get("error").textContent, /AstrBot WebUI/);
assert.equal(standalone.elements.get("start").disabled, true);
console.log("Account page bridge, QR states, redaction, confirmation and lifecycle checks passed.");
