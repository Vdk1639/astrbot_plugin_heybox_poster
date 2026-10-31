// Execute the real modules with an authenticated bridge and a small DOM; no network or account writes.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import vm from "node:vm";

const pageRoot = new URL("../pages/account/", import.meta.url);
const html = readFileSync(new URL("index.html", pageRoot), "utf8");
const sources = Object.fromEntries(readdirSync(pageRoot).filter((name) => name.endsWith(".js"))
  .map((name) => [name, readFileSync(new URL(name, pageRoot), "utf8")]));
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(setImmediate);
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

class Element {
  constructor(tag, document) {
    this.tagName = tag.toLowerCase(); this.ownerDocument = document; this.children = [];
    this.attributes = {}; this.dataset = {}; this.listeners = {}; this.className = "";
    this.hidden = false; this.disabled = false; this.checked = false; this._text = "";
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        const on = force ?? !names.has(name); if (on) names.add(name); else names.delete(name);
        this.className = [...names].join(" "); return on;
      },
      add: (name) => this.classList.toggle(name, true), remove: (name) => this.classList.toggle(name, false),
    };
  }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(""); }
  set textContent(value) { this._text = String(value ?? ""); this.children = []; }
  get options() { return this.children.filter((node) => node.tagName === "option"); }
  get value() {
    if (this.tagName === "select") return this.options.find((option) => option.selected)?.value ?? this.options[0]?.value ?? "";
    return this._value ?? (this.tagName === "option" ? this.textContent : "");
  }
  set value(value) {
    if (this.tagName === "select") this.options.forEach((option) => { option.selected = option.value === String(value); });
    else this._value = String(value);
  }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { this._text = ""; this.children = []; this.append(...nodes); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    else if (name === "class") this.className = String(value);
    else if (["hidden", "disabled", "required", "checked", "selected"].includes(name)) this[name] = true;
    else this[name] = String(value);
  }
  removeAttribute(name) { delete this.attributes[name]; delete this[name]; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  async emit(name, extra = {}) {
    const event = { target: this, currentTarget: this, preventDefault() { this.defaultPrevented = true; }, ...extra };
    await Promise.all((this.listeners[name] || []).map((handler) => handler(event)));
  }
  focus() { this.ownerDocument.activeElement = this; }
  select() {}
  setCustomValidity(message) { this.validationMessage = message; }
  reportValidity() { return !this.validationMessage; }
  matches(selector) {
    const not = selector.match(/:not\(([^)]+)\)/);
    if (not && this.matches(not[1])) return false;
    selector = selector.replace(/:not\([^)]+\)/, "");
    if (selector.includes(":checked") && !this.checked) return false;
    selector = selector.replace(":checked", "");
    const tag = selector.match(/^[\w-]+/);
    if (tag && this.tagName !== tag[0]) return false;
    for (const [, name] of selector.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(name)) return false;
    for (const [, name, value] of selector.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
      const actual = name.startsWith("data-") ? this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] : this.attributes[name];
      if (actual === undefined || value !== undefined && value !== actual) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    const output = [];
    const match = (node, query) => {
      const parts = query.trim().split(/\s+/); if (!node.matches(parts.pop())) return false;
      let parent = node.parentNode;
      while (parts.length) { const part = parts.pop(); while (parent && !parent.matches(part)) parent = parent.parentNode; if (!parent) return false; parent = parent.parentNode; }
      return true;
    };
    const visit = (host) => { for (const node of host.children) { if (selector.split(",").some((query) => match(node, query))) output.push(node); visit(node); } };
    visit(this); return output;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function dom() {
  const document = { elements: new Map(), listeners: {}, activeElement: null };
  document.createElement = (tag) => new Element(tag, document);
  document.root = document.createElement("document");
  document.getElementById = (id) => document.elements.get(id);
  document.querySelectorAll = (selector) => document.root.querySelectorAll(selector);
  document.querySelector = (selector) => document.root.querySelector(selector);
  document.addEventListener = (name, handler) => (document.listeners[name] ||= []).push(handler);
  const stack = [document.root];
  for (const token of html.match(/<[^>]+>|[^<]+/g)) {
    if (token.startsWith("<!")) continue;
    if (token.startsWith("</")) { stack.pop(); continue; }
    if (!token.startsWith("<")) { stack.at(-1)._text += token.trim(); continue; }
    const tag = token.match(/^<([\w-]+)/)?.[1]; if (!tag) continue;
    const node = document.createElement(tag);
    for (const [, key, value] of token.slice(tag.length + 1, -1).matchAll(/([\w-]+)(?:="([^"]*)")?/g)) node.setAttribute(key, value ?? "");
    if (node.id) { assert(!document.elements.has(node.id), `duplicate ID ${node.id}`); document.elements.set(node.id, node); }
    stack.at(-1).append(node);
    if (!["meta", "link", "input", "img", "br", "hr"].includes(tag)) stack.push(node);
  }
  return document;
}

const config = {
  profiles: [{ profile_id: "default", display_name: "默认账号" }, { profile_id: "second", display_name: "第二账号" }],
  publishing: { profile_id: "second", dry_run: true, post_review_required: true, comment_review_required: true, default_topic_ids: "42", view_limit: 2 },
  proactive_feed: { dry_run: true, topic_ids: [], fallback_sources: ["all"] },
};
const schema = {
  profiles: { type: "template_list", templates: { profile: { items: { profile_id: { type: "string", default: "default" }, display_name: { type: "string", default: "账号" } } } } },
  publishing: { type: "object", description: "发布管理", items: { profile_id: { type: "string", default: "default" }, dry_run: { type: "bool", default: true }, post_review_required: { type: "bool", default: true }, comment_review_required: { type: "bool", default: true } } },
};
function review(kind, status = "pending", id = 1) {
  return { id, kind, profile_id: "second", account_id: "uid-2", payload: { title: "<img src=x onerror=alert(1)>", text: "<script>unsafe()</script>", post_id: "post-42", parent_comment_id: "parent-4", root_comment_id: "root-2", topic_ids: ["42"], mode: "dynamic", view_limit: 2 }, source: "proactive", status, created_at: 1700000000 };
}

async function page(options = {}) {
  const document = dom();
  const calls = [], timers = new Map(), events = {}, unsubscribed = [];
  let timerId = 0;
  const mock = { get: null, post: null, subscribe: null, authenticated: false, readyCalls: 0, reloaded: false };
  const get = async (path, query = {}) => {
    if (mock.get) { const value = await mock.get(path, query); if (value !== undefined) return value; }
    if (path === "config" || path === "config/defaults") return plain(config);
    if (path === "config/schema") return plain(schema);
    if (path === "status") return { version: "v0.6.0", profiles: [], tasks: [], adapters: [], alerts: [] };
    if (path === "publishing/status") return { profile_id: "second", has_credentials: true, uid: "uid-2", dry_run: true, post_review_required: true, comment_review_required: true, counts: { pending: 2 } };
    if (path === "auth/status") return { profile_id: query.profile_id, state: "logged_in", nickname: query.profile_id, uid: query.profile_id };
    if (path === "events") return { page: 1, page_size: 50, total: 0, items: [] };
    if (path === "storage") return { counts: {} };
    if (path === "feed/topics/probe") return { topics: [{ id: "1", name: "数码", group: "技术" }], topic_count: 1, feed_verified: true, verified_topic_id: "1" };
    return { items: [] };
  };
  const host = {
    async ready() { mock.readyCalls++; await options.ready; if (options.failReady) throw new Error("认证失败"); mock.authenticated = true; },
    async apiGet(path, query) { assert(mock.authenticated, "GET before bridge.ready"); calls.push({ method: "GET", path, query: plain(query || {}) }); return get(path, query); },
    async apiPost(path, body) { assert(mock.authenticated, "POST before bridge.ready"); calls.push({ method: "POST", path, body: plain(body) }); if (mock.post) { const value = await mock.post(path, body); if (value !== undefined) return value; } return { status: "pending", review: review(body.kind || "post") }; },
    async subscribeSSE(path, handlers) { calls.push({ method: "SSE", path }); mock.handlers = handlers; return mock.subscribe ? mock.subscribe(path, handlers) : "subscription-1"; },
    unsubscribeSSE(id) { unsubscribed.push(id); },
  };
  const schedule = (callback, interval = false) => { const id = ++timerId; timers.set(id, { callback, interval }); return id; };
  const window = {
    AstrBotPluginPage: options.noBridge ? undefined : host,
    addEventListener: (name, fn) => (events[name] ||= []).push(fn),
    location: { reload: () => { mock.reloaded = true; } },
  };
  const context = vm.createContext({ window, document, navigator: {}, console, structuredClone, queueMicrotask,
    setTimeout: (fn) => schedule(fn), clearTimeout: (id) => timers.delete(id),
    setInterval: (fn) => schedule(fn, true), clearInterval: (id) => timers.delete(id),
    requestAnimationFrame: (fn) => schedule(fn), cancelAnimationFrame: (id) => timers.delete(id),
  });
  const load = (name, returned) => vm.runInContext(`(() => { ${sources[name].replace(/^import .*;\r?\n/gm, "").replace(/^export /gm, "")}\nreturn ${returned}; })()`, context, { filename: name });
  context.bridge = load("bridge.js", "bridge");
  context.createPublishing = load("publishing.js", "createPublishing");
  const app = load("app.js", "({loadStatus, loadConfig, loadLogin, loadEvents, loadCandidates, loadLogs, loadStorage, loadTab, state, publishing, renderLogin})");
  const topics = load("topic_probe.js", "({loadSources, probeTopics, saveTopicSelection, saveFallbackSources})");
  await flush();
  const $ = (id) => document.getElementById(id);
  const trigger = async (id, name = "click") => { await $(id).emit(name); await flush(); };
  const posts = () => calls.filter((call) => call.method === "POST");
  const lifecycle = (name, data = {}) => { for (const fn of events[name] || []) fn(data); };
  const submit = async (accept = true) => {
    const pending = $("publish-form").emit("submit"); await flush();
    if (!$("confirm-overlay").hidden) await trigger(accept ? "confirm-accept" : "confirm-cancel");
    await pending;
  };
  const tab = async (name) => { await document.querySelector(`.tabs button[data-tab="${name}"]`).emit("click"); await flush(); };
  return { $, app, topics, mock, document, calls, posts, timers, trigger, lifecycle, submit, tab, unsubscribed };
}

let passed = 0;
async function test(name, work) { await work(); passed++; console.log(`ok ${passed} - ${name}`); }

await test("complete reference navigation, safe DOM and poster bridge identity", async () => {
  const p = await page();
  for (const tab of ["overview", "login", "settings", "sources", "events", "feed", "logs", "storage", "publishing", "post-reviews", "comment-reviews"]) assert(p.$(tab));
  assert.match(html, /历史主动评论审核/); assert.match(html, /heybox_community/);
  for (const source of Object.values(sources)) assert.doesNotMatch(source, /innerHTML|insertAdjacentHTML|document\.write|\bfetch\s*\(|new EventSource|localStorage|sessionStorage|astrbot_plugin_xiaoheihe_adapter/);
  assert.match(sources["bridge.js"], /astrbot_plugin_heybox_poster/);
  assert.equal(p.mock.readyCalls, 1);
  assert.equal(p.$("publish-profile").options.length, 2);
  assert.equal(p.$("publish-profile").value, "second");
  assert.equal(p.$("post-topics").value, "42"); assert.equal(p.$("post-view-limit").value, "2");
  assert.match(p.$("config-form").textContent, /发布管理/);
  assert.match(p.$("publishing-status").textContent, /uid-2/);
});

await test("all requests wait for authenticated bridge; no bridge fails visibly", async () => {
  const auth = deferred(); const p = await page({ ready: auth.promise });
  const tab = p.tab("post-reviews");
  const probe = p.topics.probeTopics();
  await flush(); assert.equal(p.calls.length, 0);
  auth.resolve(); await Promise.all([tab, probe]);
  assert.equal(p.mock.readyCalls, 1); assert(p.calls.length > 0);
  const absent = await page({ noBridge: true });
  assert.equal(absent.calls.length, 0); assert.match(absent.$("hero-summary").textContent, /AstrBot WebUI/);
  const failed = await page({ failReady: true });
  assert.equal(failed.calls.length, 0); assert.match(failed.$("hero-summary").textContent, /认证失败/);
});

await test("post submission confirms a captured profile and exact typed payload", async () => {
  const p = await page();
  p.$("post-title").value = "标题"; p.$("publish-text").value = "内容";
  p.$("post-topics").value = "42, 43，42"; p.$("post-mode").value = "article"; p.$("post-draft").checked = true;
  await p.submit(false); assert.equal(p.posts().length, 0);
  const pending = p.$("publish-form").emit("submit"); await flush();
  p.$("publish-profile").value = "default"; p.$("publish-text").value = "后来修改";
  await p.$("publish-form").emit("submit");
  await p.trigger("confirm-accept"); await pending;
  assert.deepEqual(p.posts()[0], { method: "POST", path: "publishing/submit", body: { kind: "post", profile_id: "second", payload: { title: "标题", text: "内容", topic_ids: ["42", "43"], mode: "article", draft: true, view_limit: 2 } } });
  assert.equal(p.posts().length, 1); assert.match(p.$("publish-result").textContent, /审核队列/);
  p.$("publish-profile").value = "second"; p.$("publish-text").value = "内容";
  await p.submit(); assert.equal(p.posts().length, 1);
});

await test("comments use unified profiles and preserve reply targets", async () => {
  const p = await page(); p.$("publish-kind").value = "comment"; await p.trigger("publish-kind", "change");
  assert(p.$("post-fields").disabled); assert(!p.$("comment-fields").hidden);
  p.$("publish-text").value = "回复内容";
  await p.submit(); assert.equal(p.posts().length, 0); assert.match(p.$("toast").textContent, /目标帖子/);
  p.$("comment-post-id").value = "p42"; p.$("comment-parent-id").value = "c1"; p.$("comment-root-id").value = "c0";
  p.mock.post = () => ({ status: "dry_run" }); await p.submit();
  assert.deepEqual(p.posts()[0].body, { kind: "comment", profile_id: "second", payload: { post_id: "p42", text: "回复内容", parent_comment_id: "c1", root_comment_id: "c0" } });
  assert.match(p.$("publish-result").textContent, /没有实际发送/);
});

await test("dry run approval leaves pending reviews visible and actionable", async () => {
  const p = await page();
  p.mock.get = (path, query) => path === "publishing/reviews" ? { items: [review(query.kind)] } : undefined;
  p.mock.post = () => ({ status: "dry_run", review: review("post", "pending") });
  await p.tab("post-reviews");
  const operation = p.$("post-review-list").querySelector("button").emit("click");
  await flush(); await p.trigger("confirm-accept"); await operation;
  const card = p.$("post-review-list").children[0];
  assert.equal(card.dataset.reviewId, "1"); assert.match(card.textContent, /待审核/);
  assert.equal(card.querySelectorAll("button").length, 2); assert(!card.querySelector("button").disabled);
  assert.match(p.$("toast").textContent, /模拟运行.*仍保留为待审核/);
});

await test("credential-free dry run has no review and does not block a later real submission", async () => {
  const p = await page(); p.$("publish-text").value = "模拟完成后继续登录发布";
  p.mock.post = (path, body) => ({ status: "dry_run", payload: body.payload });
  await p.submit(); assert.match(p.$("publish-result").textContent, /未生成审核项/);
  assert.match(p.$("post-review-list").textContent, /暂无/);
  p.mock.post = () => ({ status: "pending", review: review("post") });
  await p.submit(); assert.equal(p.posts().length, 2);
  assert.match(p.$("publish-result").textContent, /审核队列/);
});

await test("unknown or lost send responses never offer resubmission", async () => {
  for (const outcome of ["send_unknown", "network", "unrecognized"]) {
    const p = await page(); p.$("publish-text").value = "可能已经发出";
    p.mock.post = () => { if (outcome === "network") throw new Error("network"); return { status: outcome }; };
    await p.submit(); assert(p.$("publish-submit").disabled); assert.match(p.$("publish-result").textContent, /不能.*重发/);
    p.$("publish-text").value = "改文也不能绕过未知状态"; await p.submit(); assert.equal(p.posts().length, 1);
  }
});

await test("refresh failure preserves confirmed send and duplicate guard", async () => {
  const p = await page(); p.$("publish-text").value = "已发送内容"; p.mock.post = () => ({ status: "sent" });
  p.mock.get = (path) => { if (path === "publishing/reviews") throw new Error("refresh unavailable"); };
  await p.submit(); assert.match(p.$("publish-result").textContent, /发送成功/);
  await p.submit(); assert.equal(p.posts().length, 1);
});

await test("review cards show account, source, content and target; unknown is read only", async () => {
  const p = await page();
  p.mock.get = (path, query) => path === "publishing/reviews" ? { items: [review(query.kind, "pending", 1), review(query.kind, "send_unknown", 2), review(query.kind, "failed", 3)] } : undefined;
  await p.tab("comment-reviews"); const host = p.$("comment-review-list");
  assert.match(host.textContent, /second.*uid-2/); assert.match(host.textContent, /主动评论/);
  assert.match(host.textContent, /post-42.*parent-4.*root-2/); assert.match(host.textContent, /<script>unsafe\(\)<\/script>/);
  assert.equal(host.querySelectorAll("script, img").length, 0);
  assert.equal(host.children[0].querySelectorAll("button").length, 2);
  assert.equal(host.children[1].querySelectorAll("button").length, 0);
  assert.match(host.children[1].textContent, /不能批准、拒绝或重发/);
  assert.equal(host.children[2].querySelectorAll("button").length, 0);
  assert.match(host.children[2].textContent, /发送失败/);
  const approve = host.children[0].querySelectorAll("button")[0];
  const operation = approve.emit("click"); await flush(); assert.equal(p.posts().length, 0);
  await p.trigger("confirm-accept"); await operation;
  assert.deepEqual(p.posts()[0], { method: "POST", path: "publishing/reviews/1/approve", body: { confirm: true } });
});

await test("review actions are mutually exclusive, reject uses empty body, IDs are encoded", async () => {
  const p = await page();
  p.mock.get = (path, query) => path === "publishing/reviews" ? { items: [review(query.kind, "pending", "a/b")] } : undefined;
  await p.app.publishing.loadReviews("post");
  const [approve, reject] = p.$("post-review-list").querySelectorAll("button");
  const operation = approve.emit("click"); await flush();
  await reject.emit("click"); assert.equal(p.posts().length, 0);
  await p.trigger("confirm-cancel"); await operation; assert(!reject.disabled);
  await reject.emit("click"); assert.deepEqual(p.posts()[0], { method: "POST", path: "publishing/reviews/a%2Fb/reject", body: {} });
});

await test("review history uses one all-status request and labels terminal sources", async () => {
  const p = await page();
  p.mock.get = (path, query) => path === "publishing/reviews"
    ? { items: [
        review(query.kind, "sent", 1),
        { ...review(query.kind, "rejected", 2), source: "notification" },
        { ...review(query.kind, "failed", 3), source: "dashboard", error: "发送被拒绝 (ValueError)" },
        { ...review(query.kind, "pending", 4), source: "legacy_candidate" },
      ] } : undefined;
  await p.app.publishing.loadReviews("comment");
  const reviewCalls = p.calls.filter((call) => call.path === "publishing/reviews");
  assert.equal(reviewCalls.length, 1); assert.deepEqual(reviewCalls[0].query, { kind: "comment" });
  const host = p.$("comment-review-list");
  assert.match(host.textContent, /已发送/); assert.match(host.textContent, /已拒绝/); assert.match(host.textContent, /发送失败.*发送被拒绝/);
  assert.match(host.textContent, /管理面板/); assert.match(host.textContent, /通知回复/); assert.match(host.textContent, /历史主动候选/);
  assert.equal(host.querySelectorAll("button").length, 2);
});

await test("stale reviews cannot replace newer responses", async () => {
  const p = await page(); const old = deferred(); let requests = 0;
  p.mock.get = (path, query) => {
    if (path !== "publishing/reviews") return;
    return ++requests === 1 ? old.promise : { items: [review(query.kind, "pending", 99)] };
  };
  const first = p.app.publishing.loadReviews("post"); await flush();
  await p.app.publishing.loadReviews("post"); old.resolve({ items: [review("post", "pending", 1)] }); await first;
  assert.equal(p.$("post-review-list").children[0].dataset.reviewId, "99");
});

await test("login profile races, QR URI validation and expiration", async () => {
  const p = await page(); const old = deferred();
  p.mock.get = (path, query) => path === "auth/status" && query.profile_id === "default" ? old.promise : undefined;
  const first = p.app.loadLogin(); await flush();
  p.$("login-profile").value = "second"; await p.trigger("login-profile", "change");
  old.resolve({ state: "logged_in", nickname: "过期旧账号" }); await first;
  assert.match(p.$("login-facts").textContent, /second/); assert.doesNotMatch(p.$("login-facts").textContent, /过期旧账号/);
  p.app.renderLogin({ state: "waiting_scan", qr_image: "javascript:alert(1)", expires_at: Date.now() / 1000 + 30 }); assert(p.$("qr-image").hidden);
  p.app.renderLogin({ state: "waiting_scan", qr_image: "data:image/png;base64,AAAA", expires_at: Date.now() / 1000 - 1 }); assert(p.$("qr-image").hidden);
  p.app.renderLogin({ state: "waiting_scan", qr_image: "data:image/png;base64,AAAA", expires_at: Date.now() / 1000 + 30 }); assert(!p.$("qr-image").hidden);
});

await test("logout requires confirmation and binds to the displayed profile", async () => {
  const p = await page(); p.$("login-profile").value = "second";
  const cancelled = p.$("logout").emit("click"); await flush(); assert.equal(p.posts().length, 0);
  await p.trigger("confirm-cancel"); await cancelled; assert.equal(p.posts().length, 0);
  const operation = p.$("logout").emit("click"); await flush();
  assert.match(p.$("confirm-message").textContent, /second/);
  await p.$("logout").emit("click"); await p.trigger("confirm-accept"); await operation;
  assert.deepEqual(p.posts()[0], { method: "POST", path: "auth/logout", body: { profile_id: "second", confirm: true } });
  assert.equal(p.posts().length, 1);
});

await test("legacy proactive candidates remain accessible and confirm edited text", async () => {
  const p = await page(); p.mock.get = (path, query) => path === "feed/candidates" ? { items: query.status === "pending" ? [{ id: 7, profile_id: "second", post_id: "42", generated_text: "旧候选", status: "pending" }] : [] } : undefined;
  p.mock.post = () => ({ approved: true, result: "dry_run" }); await p.tab("feed");
  const card = p.$("candidate-list").children[0]; card.querySelector("textarea").value = "审核后的评论";
  const operation = card.querySelector("button").emit("click"); await flush(); assert.equal(p.posts().length, 0);
  await p.trigger("confirm-accept"); await operation;
  assert.deepEqual(p.posts()[0].body, { edited_text: "审核后的评论", confirm: true });
  p.mock.get = (path, query) => path === "feed/candidates" ? { items: query.status === "send_unknown" ? [{ id: 8, profile_id: "second", post_id: "42", generated_text: "可能已发送", status: "send_unknown" }] : [] } : undefined;
  await p.app.loadCandidates();
  assert.equal(p.$("candidate-list").querySelectorAll("button").length, 0);
  assert.match(p.$("candidate-list").textContent, /不能重发/);
});

await test("settings save the shared profile schema and validate controls", async () => {
  const p = await page();
  const name = p.$("config-form").querySelector('input[data-config-key="display_name"]');
  name.value = "修改后的档案"; await name.emit("input");
  name.setCustomValidity("invalid"); await p.trigger("save-config"); assert.equal(p.posts().length, 0);
  name.setCustomValidity(""); p.mock.post = () => ({ changed: ["publishing"] });
  await p.trigger("save-config");
  assert.equal(p.posts()[0].path, "config/save");
  assert.equal(p.posts()[0].body.profiles[0].display_name, "修改后的档案");
  assert.equal(p.posts()[0].body.publishing.comment_review_required, true);
  assert.match(p.$("config-result").textContent, /publishing/);
});

await test("event filters, logs, diagnostics and storage keep adapter API contracts", async () => {
  const p = await page();
  p.mock.get = (path) => {
    if (path === "events") return { page: 1, page_size: 50, total: 1, items: [{ content: "<img onerror=bad()>", post_id: "target", event_type: "reply", status: "sent" }] };
    if (path === "logs") return { items: [{ time: 1700000000, level: "INFO", profile_id: "second", message: "日志内容" }] };
    if (path === "diagnostics") return { state: "sanitized" };
    if (path === "storage") return { database_size: 1024, log_size: 2048, counts: { events: 1 } };
    if (path === "storage/cleanup-preview") return { expired: 3 };
  };
  p.$("event-uid").value = "123"; p.$("event-post").value = "target"; p.$("event-keyword").value = "query";
  await p.trigger("search-events");
  assert.deepEqual(p.calls.find((call) => call.path === "events").query, { status: "", uid: "123", post_id: "target", keyword: "query", start_time: "", end_time: "", page: 1, page_size: 50 });
  assert.match(p.$("event-table").textContent, /<img onerror=bad\(\)>/); assert.equal(p.$("event-table").querySelectorAll("img").length, 0);
  await p.trigger("search-logs"); assert.match(p.$("log-output").textContent, /日志内容/);
  await p.trigger("copy-diagnostics"); assert.match(p.$("diagnostics-output").value, /sanitized/); assert(!p.$("diagnostics-fallback").hidden);
  await p.tab("storage"); assert.match(p.$("storage-cards").textContent, /1.0 KB/);
  await p.trigger("preview-cleanup"); assert.match(p.$("cleanup-output").textContent, /expired/);
  const cleanup = p.$("run-cleanup").emit("click"); await flush(); assert.equal(p.posts().length, 0);
  await p.trigger("confirm-accept"); await cleanup;
  assert.deepEqual(p.posts()[0], { method: "POST", path: "storage/cleanup", body: { confirm: true } });
});

await test("source save keeps other config groups and cancels delayed reload on unload", async () => {
  const p = await page(); await p.topics.loadSources();
  const checkbox = p.$("fallback-source-list").querySelector('input[data-fallback-source="pc_game"]');
  checkbox.checked = true; await checkbox.emit("change");
  p.mock.post = () => ({ changed: ["proactive_feed"] });
  await p.topics.saveFallbackSources();
  assert.equal(p.posts()[0].path, "config/save");
  assert.deepEqual(p.posts()[0].body.proactive_feed.fallback_sources, ["pc_game"]);
  assert.deepEqual(p.posts()[0].body.publishing, config.publishing);
  p.lifecycle("pagehide"); assert.equal(p.timers.size, 0); assert(!p.mock.reloaded);
});

await test("source probes cannot overwrite a newly selected profile", async () => {
  const p = await page(); const old = deferred();
  p.mock.get = (path, query) => path === "feed/topics/probe" && query.profile_id === "default" ? old.promise : undefined;
  const first = p.topics.probeTopics(); await flush(); p.$("login-profile").value = "second";
  await p.trigger("login-profile", "change"); await p.topics.loadSources();
  old.resolve({ topic_count: 999, topics: [{ id: "old", name: "旧分区" }], feed_verified: true }); await first;
  assert.doesNotMatch(p.$("topic-probe-status").textContent, /999|旧分区/); assert.match(p.$("topic-probe-status").textContent, /1 个真实分区/);
});

await test("SSE drops late subscriptions and ignores callbacks after navigation", async () => {
  const p = await page(); const connection = deferred(); p.mock.subscribe = () => connection.promise;
  const logs = p.tab("logs"); await flush(); const callbacks = p.mock.handlers;
  await p.tab("overview"); connection.resolve("late-subscription"); await logs;
  assert.deepEqual(p.unsubscribed, ["late-subscription"]);
  callbacks.onMessage({ parsed: { type: "log", entry: { time: 0, level: "INFO", message: "late log" } } });
  assert.equal(p.app.state.logs.length, 0);
});

await test("pagehide cleans timers, SSE and confirmations, rejects late data and BFCache restores", async () => {
  const p = await page(); await p.tab("logs");
  p.mock.handlers.onMessage({ parsed: { type: "log", entry: { time: 0, level: "INFO", message: "new log" } } });
  p.app.renderLogin({ state: "waiting_scan", qr_image: "data:image/png;base64,AAAA", expires_at: Date.now() / 1000 + 60 });
  const response = deferred(); p.mock.get = (path) => path === "publishing/status" ? response.promise : undefined;
  const late = p.app.publishing.loadStatus(); const before = p.$("publishing-status").textContent;
  const logout = p.$("logout").emit("click"); await flush();
  p.lifecycle("pagehide"); await logout;
  response.resolve({ profile_id: "late", counts: {} }); await assert.rejects(late);
  assert.equal(p.$("publishing-status").textContent, before);
  assert.equal(p.timers.size, 0); assert(p.unsubscribed.includes("subscription-1")); assert(p.$("confirm-overlay").hidden);
  const callCount = p.calls.length; await p.trigger("check-login"); assert.equal(p.calls.length, callCount);
  p.lifecycle("pageshow", { persisted: true }); assert(p.mock.reloaded);
});

console.log(`${passed} dashboard checks passed`);
