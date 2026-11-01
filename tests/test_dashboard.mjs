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
  click() { return this.emit("click"); }
  async emit(name, extra = {}) {
    const event = { target: this, currentTarget: this, preventDefault() { this.defaultPrevented = true; }, ...extra };
    await Promise.all((this.listeners[name] || []).map((handler) => handler(event)));
  }
  focus() { this.ownerDocument.activeElement = this; }
  select() {}
  setCustomValidity(message) { this.validationMessage = message; }
  reportValidity() { return !this.validationMessage; }
  checkValidity() { return this.reportValidity(); }
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
  document.dispatchEvent = (event) => { for (const handler of document.listeners[event.type] || []) handler(event); return true; };
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
  reply: { reply_thread_once: true, reply_daily_per_user: 5, reply_daily_total: 30, reply_owner_exempt: true },
  risk: { enabled: true, auto_filter_enabled: false, watch_score: 20, auto_filter_score: 60, flood_window_minutes: 10, flood_threshold: 8, burst_score: 20, repeat_score: 15, injection_score: 25, privacy_score: 30, owner_exempt: true },
};
config.publishing.comment_daily_limit = 30;
const actualSchema = JSON.parse(readFileSync(new URL("../_conf_schema.json", import.meta.url), "utf8"));
const schema = {
  profiles: { type: "template_list", templates: { profile: { items: { profile_id: { type: "string", default: "default" }, display_name: { type: "string", default: "账号" } } } } },
  publishing: { type: "object", description: "发布管理", items: { profile_id: { type: "string", default: "default" }, dry_run: { type: "bool", default: true }, post_review_required: { type: "bool", default: true }, comment_review_required: { type: "bool", default: true } } },
};
schema.reply = actualSchema.reply;
schema.risk = actualSchema.risk;
schema.publishing.items.comment_daily_limit = actualSchema.publishing.items.comment_daily_limit;
schema.publishing.items.view_limit = actualSchema.publishing.items.view_limit;
schema.publishing.items.default_topic_ids = actualSchema.publishing.items.default_topic_ids;
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
    if (path === "status") return { version: "v0.8.1", profiles: [], tasks: [], adapters: [], alerts: [] };
    if (path === "publishing/status") return { profile_id: "second", has_credentials: true, uid: "uid-2", dry_run: true, post_review_required: true, comment_review_required: true, counts: { pending: 2 } };
    if (path === "auth/status") return { profile_id: query.profile_id, state: "logged_in", nickname: query.profile_id, uid: query.profile_id };
    if (path === "events") return { page: 1, page_size: 50, total: 0, items: [] };
    if (path === "risk/users") return { page: 1, page_size: 30, total: 0, items: [] };
    if (path === "risk/users/detail") return { user: { profile_id: query.profile_id, sender_uid: query.uid, score: 0, level: "normal", manual_blocked: false, auto_filtered: false }, events: { page: query.page, page_size: 30, total: 0, items: [] } };
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
  const context = vm.createContext({ window, document, Node: Element, navigator: {}, console, structuredClone, queueMicrotask,
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    setTimeout: (fn) => schedule(fn), clearTimeout: (id) => timers.delete(id),
    setInterval: (fn) => schedule(fn, true), clearInterval: (id) => timers.delete(id),
    requestAnimationFrame: (fn) => schedule(fn), cancelAnimationFrame: (id) => timers.delete(id),
  });
  const load = (name, returned) => vm.runInContext(`(() => { ${sources[name].replace(/^import .*;\r?\n/gm, "").replace(/^export /gm, "")}\nreturn ${returned}; })()`, context, { filename: name });
  context.bridge = load("bridge.js", "bridge");
  context.createPublishing = load("publishing.js", "createPublishing");
  const app = load("app.js", "({loadStatus, loadConfig, loadLogin, loadEvents, loadRiskUsers, loadRiskDetail, loadCandidates, loadLogs, loadStorage, loadTab, state, publishing, renderLogin})");
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
  assert.match(html, /主动评论候选/); assert.match(html, /heybox_community/);
  for (const source of Object.values(sources)) assert.doesNotMatch(source, /innerHTML|insertAdjacentHTML|document\.write|\bfetch\s*\(|new EventSource|localStorage|sessionStorage|astrbot_plugin_xiaoheihe_adapter/);
  assert.match(sources["bridge.js"], /astrbot_plugin_heybox_poster/);
  assert.equal(p.mock.readyCalls, 1);
  assert.equal(p.$("publish-profile").options.length, 2);
  assert.equal(p.$("publish-profile").value, "second");
  assert.equal(p.$("post-topics").value, "42"); assert.equal(p.$("post-view-limit").value, "2");
  assert.match(p.$("config-form").textContent, /发布管理/);
  assert.match(p.$("publishing-status").textContent, /uid-2/);
});

await test("overview renders full errors as separate details without enlarging ordinary metrics", async () => {
  const p = await page();
  const message = "请求失败，请稍后核对。<img src=x onerror=bad()> " + "upstream_timeout_".repeat(12);
  for (const profile of [{ last_error: message, last_client_error: { message: "older error" } },
    { last_client_error: { message } }, {}]) {
    p.mock.get = (path) => path === "status" ? { profiles: [profile], tasks: [], adapters: [], alerts: [] } : undefined;
    await p.app.loadStatus();
    const cards = p.$("status-cards");
    const error = cards.querySelectorAll(".metric").find((card) => card.querySelector(".label").textContent === "最近错误");
    const hasError = Boolean(profile.last_error || profile.last_client_error);
    assert.equal(error.classList.contains("metric-detail"), hasError);
    assert.equal(error.querySelector(".value").textContent, hasError ? message : "—");
    assert.equal(cards.querySelectorAll(".metric-detail").length, hasError ? 1 : 0);
    assert.equal(cards.querySelectorAll("img").length, 0);
  }
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
        { ...review(query.kind, "pending", 4), source: "proactive_candidate" },
      ] } : undefined;
  await p.app.publishing.loadReviews("comment");
  const reviewCalls = p.calls.filter((call) => call.path === "publishing/reviews");
  assert.equal(reviewCalls.length, 1); assert.deepEqual(reviewCalls[0].query, { kind: "comment" });
  const host = p.$("comment-review-list");
  assert.match(host.textContent, /已发送/); assert.match(host.textContent, /已拒绝/); assert.match(host.textContent, /发送失败.*发送被拒绝/);
  assert.match(host.textContent, /管理面板/); assert.match(host.textContent, /通知回复/); assert.match(host.textContent, /主动浏览候选/);
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

await test("proactive candidates remain accessible and confirm edited text", async () => {
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

await test("settings separate long labels from full guidance using safe text", async () => {
  const p = await page(), localSchema = plain(schema);
  localSchema.reply.items.reply_daily_per_user = { ...localSchema.reply.items.reply_daily_per_user,
    description: "每日处理次数；0 不限；失败/模拟/审核拒绝不退", hint: "<img src=x onerror=bad()>" };
  p.mock.get = (path) => path === "config/schema" ? localSchema : undefined;
  await p.app.loadConfig();
  const form = p.$("config-form");
  const field = form.querySelector('input[data-config-key="reply_daily_per_user"]').parentNode;
  assert.equal(field.querySelector(".config-label").textContent, "每日处理次数");
  assert.equal(field.querySelector(".config-hint").textContent, "0 不限；失败/模拟/审核拒绝不退\n<img src=x onerror=bad()>");
  const toggle = form.querySelectorAll("label.config-toggle").find((row) => row.querySelector("strong").textContent === "同一楼层只接受一次通知回复");
  assert.equal(toggle.querySelector(".config-hint").textContent, actualSchema.reply.items.reply_thread_once.description.split("；").slice(1).join("；"));
  assert.equal(form.querySelectorAll("img").length, 0);
});

await test("post visibility settings use labelled choices and save numeric values", async () => {
  const p = await page();
  let saved = plain(config);
  p.mock.get = (path) => path === "config" ? plain(saved) : undefined;
  p.mock.post = (path, body) => {
    if (path === "config/save") { saved = plain(body); return { changed: ["publishing"] }; }
  };
  const control = () => p.$("config-form").querySelector('select[data-config-key="view_limit"]');
  assert(control());
  assert.equal(control().value, "2");
  assert.deepEqual(control().children.map((option) => [option.value, option.textContent]),
    [["1", "公开可见"], ["2", "粉丝可见"], ["3", "仅自己可见"]]);
  for (const value of [1, 3, 2]) {
    control().value = String(value); await control().emit("change");
    await p.trigger("save-config");
    assert.equal(p.posts().at(-1).body.publishing.view_limit, value);
    assert.equal(control().value, String(value));
  }
});

await test("default communities search by name, keep existing IDs and save multiple removable selections", async () => {
  const p = await page();
  let saved = plain(config);
  p.mock.get = (path) => path === "config" ? plain(saved) : path === "publishing/topics" ? { items: [
    { topic_id: "42", name: "原社区" }, { topic_id: "43", name: "新社区" },
    { topic_id: "43", name: "重复结果" }, { topic_id: "44", name: "<img src=x>" },
  ] } : undefined;
  p.mock.post = (path, body) => { if (path === "config/save") { saved = plain(body); return { changed: ["publishing"] }; } };
  const form = p.$("config-form");
  const input = form.querySelector('input[data-config-key="default_topic_search"]');
  const choices = form.querySelector('select[data-config-key="default_topic_ids"]');
  const search = form.querySelectorAll("button").find((b) => b.textContent === "搜索社区");
  const remove = (name) => p.$("config-form").querySelectorAll("button").find((b) => b.attributes["aria-label"] === `移除默认社区：${name}`);
  assert(choices.disabled); assert(remove("已保存社区（ID 42）"));
  assert(!p.calls.some((call) => call.path === "publishing/topics"));
  input.value = "  社区  "; await search.emit("click");
  assert.deepEqual(p.calls.find((call) => call.path === "publishing/topics").query, { profile_id: "second", keyword: "社区" });
  assert.deepEqual(choices.options.map((o) => o.textContent), ["请选择社区，可添加多个", "原社区", "新社区", "<img src=x>"]);
  assert(choices.options.find((o) => o.value === "42").disabled);
  assert.equal(p.app.state.config.publishing.default_topic_ids, "42");
  for (const id of ["43", "44", "43"]) { choices.value = id; await choices.emit("change"); }
  assert.equal(p.app.state.config.publishing.default_topic_ids, "42,43,44");
  assert.equal(form.querySelectorAll("img").length, 0);
  await remove("原社区").emit("click");
  assert(!choices.options.find((o) => o.value === "42").disabled);
  assert.equal(p.posts().length, 0);
  await p.trigger("save-config");
  assert.equal(saved.publishing.default_topic_ids, "43,44");
  assert(remove("新社区")); assert(remove("<img src=x>"));
  await remove("新社区").emit("click"); await remove("<img src=x>").emit("click");
  await p.trigger("save-config"); assert.equal(saved.publishing.default_topic_ids, "");
});

await test("empty, failed and retried community lookups never clear saved selections", async () => {
  const p = await page(), form = p.$("config-form");
  const input = form.querySelector('input[data-config-key="default_topic_search"]');
  const choices = form.querySelector('select[data-config-key="default_topic_ids"]');
  const search = form.querySelectorAll("button").find((b) => b.textContent === "搜索社区");
  await search.emit("click");
  assert(!p.calls.some((call) => call.path === "publishing/topics"));
  input.value = "不存在"; await input.emit("keydown", { key: "Enter" });
  assert(choices.disabled); assert.match(form.textContent, /未找到社区/);
  p.mock.get = (path) => { if (path === "publishing/topics") throw new Error("offline"); };
  await search.emit("click"); assert(!search.disabled); assert.match(form.textContent, /搜索失败/);
  assert.equal(p.app.state.config.publishing.default_topic_ids, "42");
  p.mock.get = (path) => path === "publishing/topics" ? { items: [{ topic_id: "43", name: "恢复的社区" }] } : undefined;
  await search.emit("click"); assert(!choices.disabled);
  choices.value = "43"; await choices.emit("change");
  assert.equal(p.app.state.config.publishing.default_topic_ids, "42,43");
  assert.equal(p.posts().length, 0);
});

await test("late community lookups are ignored after config reload, profile changes and unload", async () => {
  for (const action of ["reload", "profile", "unload"]) {
    const p = await page(), response = deferred(), form = p.$("config-form");
    const input = form.querySelector('input[data-config-key="default_topic_search"]');
    const choices = form.querySelector('select[data-config-key="default_topic_ids"]');
    const search = form.querySelectorAll("button").find((b) => b.textContent === "搜索社区");
    p.mock.get = (path) => path === "publishing/topics" ? response.promise : undefined;
    input.value = "社区"; const pending = search.emit("click"); await flush();
    if (action === "reload") await p.app.loadConfig();
    if (action === "profile") p.app.state.config.publishing.profile_id = "default";
    if (action === "unload") p.lifecycle("pagehide");
    response.resolve({ items: [{ topic_id: "99", name: "迟到社区" }] }); await pending;
    assert(!p.app.state.topicNames.has("99"));
    assert(!choices.options.some((o) => o.value === "99"));
    assert.equal(p.app.state.config.publishing.default_topic_ids, "42");
  }
});

await test("community choices from an earlier publishing profile cannot be added", async () => {
  const p = await page(), form = p.$("config-form");
  p.mock.get = (path) => path === "publishing/topics" ? { items: [{ topic_id: "99", name: "社区" }] } : undefined;
  form.querySelector('input[data-config-key="default_topic_search"]').value = "社区";
  await form.querySelectorAll("button").find((b) => b.textContent === "搜索社区").emit("click");
  p.app.state.config.publishing.profile_id = "default";
  const choices = form.querySelector('select[data-config-key="default_topic_ids"]');
  choices.value = "99"; await choices.emit("change");
  assert(choices.disabled); assert.equal(p.app.state.config.publishing.default_topic_ids, "42");
});

await test("settings retain both direct-reply preferences across render save and reload", async () => {
  for (const enabled of [true, false]) {
    const p = await page();
    let saved = plain(config);
    saved.reply.reply_to_direct_replies = enabled;
    p.mock.get = (path) => path === "config" ? plain(saved) : undefined;
    p.mock.post = (path, body) => {
      if (path === "config/save") { saved = plain(body); return { changed: ["reply"] }; }
    };
    await p.app.loadConfig();
    const toggle = () => p.$("config-form").querySelectorAll("label.config-toggle")
      .find((label) => label.textContent.includes(actualSchema.reply.items.reply_to_direct_replies.description)).querySelector("input");
    assert.equal(toggle().checked, enabled);
    const limit = p.$("config-form").querySelector('input[data-config-key="reply_daily_per_user"]');
    limit.value = "4"; await limit.emit("input");
    await p.trigger("save-config");
    assert.equal(p.posts()[0].body.reply.reply_to_direct_replies, enabled);
    assert.equal(p.posts()[0].body.reply.reply_daily_per_user, 4);
    assert.equal(toggle().checked, enabled);
  }
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
  assert.deepEqual(p.calls.find((call) => call.path === "events").query, { group_by: "", profile_id: "", event_id: "", status: "", uid: "123", post_id: "target", keyword: "query", start_time: "", end_time: "", page: 1, page_size: 50 });
  assert.match(p.$("event-table").textContent, /<img onerror=bad\(\)>/); assert.equal(p.$("event-table").querySelectorAll("img").length, 0);
  await p.trigger("search-logs"); assert.match(p.$("log-output").textContent, /日志内容/);
  await p.trigger("copy-diagnostics"); assert.match(p.$("diagnostics-output").value, /sanitized/); assert(!p.$("diagnostics-fallback").hidden);
  await p.tab("storage"); assert.match(p.$("storage-cards").textContent, /1.0 KB/);
  await p.trigger("preview-cleanup"); assert.match(p.$("cleanup-output").textContent, /expired/);
  const cleanup = p.$("run-cleanup").emit("click"); await flush(); assert.equal(p.posts().length, 0);
  await p.trigger("confirm-accept"); await cleanup;
  assert.deepEqual(p.posts()[0], { method: "POST", path: "storage/cleanup", body: { confirm: true } });
});

await test("source save keeps other config groups and never schedules a reload", async () => {
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

await test("guard settings retain strict numeric values and observation defaults", async () => {
  const p = await page();
  assert.match(p.$("config-form").textContent, /默认只观察/);
  const limit = p.$("config-form").querySelector('input[data-config-key="comment_daily_limit"]');
  assert.equal(limit.min, "0"); assert.equal(limit.max, "100000");
  for (const invalid of ["1.5", "-1", "100001", "", "5oops"]) {
    limit.value = invalid; await limit.emit("input");
    await p.trigger("save-config"); assert.equal(p.posts().length, 0);
  }
  limit.value = "0"; await limit.emit("input");
  p.mock.post = () => ({ changed: ["publishing"] });
  await p.trigger("save-config");
  assert.equal(p.posts()[0].body.publishing.comment_daily_limit, 0);
  assert.equal(p.posts()[0].body.risk.enabled, true);
  assert.equal(p.posts()[0].body.risk.auto_filter_enabled, false);
  assert.equal(p.posts()[0].body.reply.reply_daily_per_user, 5);
});

await test("per-profile budgets distinguish processing, real comments, shared anonymous quota and reset", async () => {
  const p = await page();
  p.mock.get = (path) => path === "status" ? { profiles: [
    { profile_id: "default", reply_budget: { processing_count: 7, processing_limit: 30, comment_count: 2, comment_limit: 0, reset_local_time: "08:00", unverified_shared: true } },
    { profile_id: "second", reply_budget: { processing_count: 7, processing_limit: 30, comment_count: 2, comment_limit: 0, reset_local_time: "08:00", unverified_shared: true } },
    { profile_id: "legacy" },
  ] } : undefined;
  await p.app.loadStatus();
  const text = p.$("reply-budgets").textContent;
  assert.match(text, /default.*今日通知处理7 [/] 30/); assert.match(text, /second.*今日真实评论2 [/] 不限/);
  assert.match(text, /08:00.*共用预算桶/); assert.match(text, /legacy.*未提供/);
});

function group(overrides = {}) {
  return { profile_id: "default", sender_uid: "123", sender_nickname: "<img onerror=bad()>", identity_verified: true, event_count: 4, post_count: 2,
    status_counts: { sent: 1, ignored: 3 }, event_type_counts: { mention: 3, proactive_feed: 1 },
    last_event_at: 1700000000, last_discovered_at: 1699999900, risk_score: 60, risk_level: "blocked", manual_blocked: false, auto_filtered: false, ...overrides };
}
function riskDetail(query, user = {}, events = {}) {
  return { user: { profile_id: query.profile_id, sender_uid: query.uid, score: 60, level: "blocked", auto_filtered: false, manual_blocked: false, note: "<script>note</script>", ...user },
    events: { page: query.page, page_size: 30, total: 31, items: [{ created_at: 1700000000, rule: "adjust", delta: -5, before_score: 65, after_score: 60, operator: "<admin>", details: { note: "<img src=x>" } }], ...events } };
}
const lastCall = (p, path) => p.calls.filter((call) => call.path === path).at(-1);

await test("group view explicitly sets seven days, drills verified or anonymous identities and restores filters and page", async () => {
  const p = await page();
  await p.tab("events");
  assert.equal(lastCall(p, "events").query.start_time, "");
  p.mock.get = (path, query) => path === "events" ? { page: query.page, page_size: 50, total: query.group_by ? 80 : 1, items: query.group_by
    ? [group(), group({ sender_uid: "", identity_verified: false, event_id: 91, risk_score: null })]
    : [{ profile_id: query.profile_id, content: "single event" }] } : undefined;
  p.$("event-mode").value = "uid"; await p.trigger("event-mode", "change");
  const range = lastCall(p, "events").query;
  assert.equal(range.group_by, "uid"); assert.equal(range.end_time - range.start_time, 7 * 86400);
  assert(Math.abs(range.end_time - Date.now() / 1000) < 65);
  assert.match(p.$("event-range-hint").textContent, /至.*浏览器本地时间/);
  assert.match(p.$("event-page").textContent, /80 组用户记录/);
  assert.match(p.$("event-table").textContent, /主动 AI 回复 1/);
  assert.match(p.$("event-table").textContent, /已达阈值，自动过滤未生效/);
  assert.match(p.$("event-table").textContent, /不能可靠归属/);
  assert.equal(p.$("event-table").querySelectorAll("img").length, 0);
  await p.trigger("event-next"); assert.equal(lastCall(p, "events").query.page, 2);
  let buttons = p.$("event-table").querySelectorAll("button");
  assert(buttons[3].disabled);
  await buttons[2].emit("click");
  let drill = lastCall(p, "events").query;
  assert.equal(drill.event_id, 91); assert.equal(drill.uid, ""); assert.equal(drill.profile_id, "default");
  assert.equal(drill.start_time, range.start_time); assert.equal(drill.end_time, range.end_time);
  await p.trigger("event-back");
  assert.equal(p.$("event-mode").value, "uid"); assert.equal(lastCall(p, "events").query.page, 2);
  buttons = p.$("event-table").querySelectorAll("button"); await buttons[0].emit("click");
  drill = lastCall(p, "events").query;
  assert.equal(drill.uid, "123"); assert.equal(drill.event_id, ""); assert.equal(drill.profile_id, "default");
  await p.trigger("event-back");
  p.$("event-start").value = ""; p.$("event-end").value = ""; await p.trigger("event-start", "change");
  p.$("event-mode").value = ""; await p.trigger("event-mode", "change");
  p.$("event-mode").value = "uid"; await p.trigger("event-mode", "change");
  assert.equal(lastCall(p, "events").query.start_time, "");
});

await test("existing time ranges survive first aggregation and filter changes reset pagination", async () => {
  const p = await page(); p.$("event-start").value = "2026-10-01T12:30";
  p.$("event-end").value = "2026-10-02T12:30";
  p.$("event-mode").value = "uid"; await p.trigger("event-mode", "change");
  assert.equal(p.$("event-start").value, "2026-10-01T12:30");
  p.app.state.eventPage = 9; p.$("event-status").value = "ignored"; await p.trigger("event-status", "change");
  assert.equal(lastCall(p, "events").query.page, 1);
  assert.equal(lastCall(p, "events").query.status, "ignored");
});

await test("risk list uses shared profile and typed filters, details paginate and history stays read only", async () => {
  const p = await page();
  p.mock.get = (path, query) => path === "risk/users" ? { page: query.page, page_size: 30, total: 31, items: [group()] }
    : path === "risk/users/detail" ? riskDetail(query) : undefined;
  p.$("event-profile").value = "default"; p.$("risk-uid").value = "123";
  p.$("risk-level").value = "blocked"; p.$("risk-manual").value = "false";
  await p.trigger("search-risk");
  assert.deepEqual(lastCall(p, "risk/users").query, { profile_id: "default", uid: "123", level: "blocked", manual_blocked: "false", page: 1, page_size: 30 });
  await p.trigger("risk-next"); assert.equal(lastCall(p, "risk/users").query.page, 2);
  await p.$("risk-users").querySelectorAll("button")[1].emit("click");
  assert(!p.$("risk-detail").hidden); assert.match(p.$("risk-facts").textContent, /自动过滤开关关闭/);
  assert.match(p.$("risk-note").textContent, /<script>note<[/]script>/);
  assert.equal(p.$("risk-detail").querySelectorAll("script, img").length, 0);
  assert.match(p.$("risk-audit").textContent, /65 → 60/);
  p.app.state.config.risk.auto_filter_enabled = true;
  await p.app.loadRiskDetail("default", "123");
  assert.match(p.$("risk-facts").textContent, /自动过滤开关关闭/);
  await p.trigger("risk-audit-next"); assert.equal(lastCall(p, "risk/users/detail").query.page, 2);
  await p.trigger("risk-audit-prev"); assert.equal(lastCall(p, "risk/users/detail").query.page, 1);
  await p.app.loadRiskDetail("deleted-profile", "123");
  assert.match(p.$("risk-result").textContent, /历史档案仅供查看/); assert(p.$("risk-block").disabled);
  await p.trigger("risk-block"); assert.equal(p.posts().length, 0);
});

await test("risk operations capture confirmation, prevent double adjustment and preserve independent filtering", async () => {
  const p = await page(); let blocked = false, score = 60, auto = true;
  p.mock.get = (path, query) => path === "risk/users/detail" ? riskDetail(query, { manual_blocked: blocked, score, auto_filtered: auto }) : undefined;
  p.mock.post = (path, body) => { if (!path.startsWith("risk/")) return;
    if (path.endsWith("/block")) blocked = true;
    if (path.endsWith("/unblock")) { blocked = false; if (body.reset_score) { score = 0; auto = false; } }
    if (path.endsWith("/adjust")) score += body.delta;
    return { user: { manual_blocked: blocked, score } };
  };
  await p.app.loadRiskDetail("default", "123");
  p.$("risk-operation-note").value = "人工核查";
  let operation = p.$("risk-block").emit("click"); await flush();
  assert.match(p.$("confirm-message").textContent, /default.*123/); assert.match(p.$("confirm-message").textContent, /人工核查/);
  await p.trigger("confirm-cancel"); await operation; assert.equal(p.posts().length, 0); assert(!p.$("risk-block").disabled);
  operation = p.$("risk-block").emit("click"); await flush(); await p.trigger("confirm-accept"); await operation;
  assert.deepEqual(p.posts()[0].body, { profile_id: "default", uid: "123", note: "人工核查", confirm: true });
  operation = p.$("risk-unblock").emit("click"); await flush(); await p.trigger("confirm-accept"); await operation;
  assert.equal(p.posts()[1].body.reset_score, false); assert.match(p.$("risk-result").textContent, /风险阈值仍阻止/);
  p.$("risk-reset").checked = true;
  operation = p.$("risk-unblock").emit("click"); await flush();
  assert.match(p.$("confirm-message").textContent, /清零分数：是/); await p.trigger("confirm-accept"); await operation;
  assert.equal(p.posts()[2].body.reset_score, true); assert.match(p.$("risk-facts").textContent, /0 分/);
  p.$("risk-delta").value = "-1.2"; await p.trigger("risk-adjust"); assert.equal(p.posts().length, 3);
  p.$("risk-operation-note").value = "x".repeat(201); await p.trigger("risk-block"); assert.equal(p.posts().length, 3);
  p.$("risk-operation-note").value = "调分备注"; p.$("risk-delta").value = "-5";
  operation = p.$("risk-adjust").emit("click"); await flush();
  p.$("risk-delta").value = "100"; await p.trigger("risk-adjust"); await p.trigger("confirm-accept"); await operation;
  assert.deepEqual(p.posts()[3].body, { profile_id: "default", uid: "123", note: "调分备注", confirm: true, delta: -5 });
  assert.equal(p.posts().length, 4);
});

await test("failed risk adjustment stays locked until an explicit audit refresh and never retries automatically", async () => {
  const p = await page(); await p.app.loadRiskDetail("default", "123");
  p.mock.post = () => { throw new Error("lost response"); };
  p.$("risk-delta").value = "5";
  const operation = p.$("risk-adjust").emit("click"); await flush(); await p.trigger("confirm-accept"); await operation;
  assert.equal(p.posts().length, 1); assert(p.$("risk-adjust").disabled);
  assert.match(p.$("risk-result").textContent, /核对审计.*勿直接重复调分/);
  await p.trigger("risk-adjust"); assert.equal(p.posts().length, 1);
  await p.trigger("refresh-risk"); assert(!p.$("risk-adjust").disabled);
});

await test("profile changes invalidate events, risk lists, detail and open confirmations", async () => {
  const p = await page(); const lateEvents = deferred(), lateList = deferred(), lateDetail = deferred();
  p.mock.get = (path) => path === "events" ? lateEvents.promise : path === "risk/users" ? lateList.promise
    : path === "risk/users/detail" ? lateDetail.promise : undefined;
  const requests = [p.app.loadEvents(), p.app.loadRiskUsers(), p.app.loadRiskDetail("default", "123")];
  await flush(); p.mock.get = null; p.$("event-profile").value = "second"; await p.trigger("event-profile", "change");
  lateEvents.resolve({ page: 1, page_size: 50, total: 1, items: [group({ content: "stale event" })] });
  lateList.resolve({ page: 1, page_size: 30, total: 1, items: [group({ sender_uid: "stale uid" })] });
  lateDetail.resolve(riskDetail({ profile_id: "default", uid: "123", page: 1 })); await Promise.all(requests);
  assert(p.$("risk-detail").hidden); assert.equal(p.app.state.riskSelected, null);
  assert.doesNotMatch(p.$("event-table").textContent, /stale/); assert.doesNotMatch(p.$("risk-users").textContent, /stale/);
  await p.app.loadRiskDetail("second", "456");
  const operation = p.$("risk-block").emit("click"); await flush();
  p.$("event-profile").value = "default"; await p.trigger("event-profile", "change"); await operation;
  assert(p.$("confirm-overlay").hidden); assert.equal(p.posts().length, 0);
});

await test("risk detail and writes cannot update DOM or send after pagehide", async () => {
  const p = await page(); await p.app.loadRiskDetail("default", "123");
  const operation = p.$("risk-block").emit("click"); await flush(); p.lifecycle("pagehide"); await operation;
  assert.equal(p.posts().length, 0); assert(p.$("confirm-overlay").hidden); assert.equal(p.timers.size, 0);
  const q = await page(), late = deferred(); q.mock.get = (path) => path === "risk/users/detail" ? late.promise : undefined;
  const request = q.app.loadRiskDetail("default", "123"); await flush();
  const before = q.$("risk-facts").textContent; q.lifecycle("pagehide");
  late.resolve(riskDetail({ profile_id: "default", uid: "123", page: 1 })); await assert.rejects(request);
  assert.equal(q.$("risk-facts").textContent, before);
});

await test("late risk detail for another user and an in-flight write cannot overwrite the selected identity", async () => {
  const p = await page(), old = deferred();
  p.mock.get = (path, query) => path === "risk/users/detail" ? query.uid === "123" ? old.promise : riskDetail(query) : undefined;
  const first = p.app.loadRiskDetail("default", "123"); await flush();
  await p.app.loadRiskDetail("second", "456");
  old.resolve(riskDetail({ profile_id: "default", uid: "123", page: 1 })); await first;
  assert.match(p.$("risk-facts").textContent, /second [/] 456/);
  const response = deferred(); p.mock.post = () => response.promise;
  const action = p.$("risk-block").emit("click"); await flush(); await p.trigger("confirm-accept");
  await p.app.loadRiskDetail("second", "789");
  const before = p.$("risk-facts").textContent;
  response.resolve({ user: { manual_blocked: true } }); await action;
  assert.equal(p.$("risk-facts").textContent, before); assert.match(before, /second [/] 789/);
  assert.equal(lastCall(p, "risk/users/block").body.uid, "456");
});

await test("local policy rejection stays pending, names the reason and restores both buttons after refresh failure", async () => {
  const p = await page(); let refreshFailed = false;
  p.mock.get = (path) => { if (path === "publishing/reviews") { if (refreshFailed) throw new Error("offline refresh"); return { items: [review("comment")] }; } };
  p.mock.post = () => ({ status: "pending", policy_blocked: true, reason: "comment_account_daily_limit", message: "账号今日评论发送次数已达上限", review: review("comment") });
  await p.tab("comment-reviews"); const card = p.$("comment-review-list").children[0];
  const [approve, reject] = card.querySelectorAll("button"); refreshFailed = true;
  let operation = approve.emit("click"); await flush(); await p.trigger("confirm-accept"); await operation;
  assert.match(card.textContent, /未发送，仍待审核.*账号今日评论发送次数已达上限/);
  assert(!approve.disabled); assert(!reject.disabled);
  p.mock.post = () => ({ status: "sent", review: review("comment", "sent") });
  operation = approve.emit("click"); await flush(); await p.trigger("confirm-accept"); await operation;
  assert.match(card.textContent, /发送成功/); assert(approve.disabled); assert(reject.disabled); assert.equal(p.posts().length, 2);
});

await test("automatic approval policy rejection shows pending in submission and lost approval responses stay disabled", async () => {
  const p = await page(); p.$("publish-text").value = "策略阻止的提交";
  p.mock.post = () => ({ status: "pending", policy_blocked: true, reason: "risk_manual_blocked", message: "用户已被人工封禁", review: review("post") });
  await p.submit(); assert.match(p.$("publish-result").textContent, /未发送，仍待审核.*人工封禁/);
  const q = await page(); q.mock.get = (path) => path === "publishing/reviews" ? { items: [review("comment")] } : undefined;
  q.mock.post = () => { throw new Error("lost approval"); }; await q.tab("comment-reviews");
  const card = q.$("comment-review-list").children[0], [approve, reject] = card.querySelectorAll("button");
  const operation = approve.emit("click"); await flush(); await q.trigger("confirm-accept"); await operation;
  assert(approve.disabled); assert(reject.disabled); assert.match(card.textContent, /不能直接重试发送/);
});

await test("event rows explain risk evaluation and behavior eligibility without inventing zero scores for skipped or legacy events", async () => {
  const p = await page();
  p.mock.get = (path) => path === "events" ? { page: 1, page_size: 50, total: 6, items: [
    { risk_state: "evaluated", risk_behavior_eligible: 0, risk_evaluated_at: 1700000000, risk_delta: 25, risk_score_after: 45, risk_hits: ["inject_override", "<img src=x>"], reason: "<script>unsafe()</script>" },
    { risk_state: "evaluated", risk_behavior_eligible: true, risk_evaluated_at: 1700000000, risk_delta: 0, risk_score_after: 0, risk_hits: [] },
    { risk_state: "skipped_legacy", risk_behavior_eligible: false, risk_delta: 0, risk_score_after: 0 },
    { risk_state: "skipped_disabled", risk_behavior_eligible: 1, risk_delta: 0, risk_score_after: 0 },
    { risk_state: "pending", risk_behavior_eligible: null, risk_delta: 0, risk_score_after: 0 },
    {},
  ] } : undefined;
  await p.app.loadEvents();
  const rows = p.$("event-table").querySelectorAll("tbody tr");
  assert.match(rows[0].textContent, /已评估.*本事件增分 25.*评估后分数 45.*inject_override/);
  assert.match(rows[0].textContent, /历史回填或时间不明，未计短时风险/);
  assert.match(rows[0].textContent, /原因：<script>unsafe/);
  assert.match(rows[1].textContent, /已评估.*本事件增分 0.*评估后分数 0.*命中 无/);
  assert.match(rows[1].textContent, /具备短时行为资格/);
  assert.match(rows[2].textContent, /旧库未评估/);
  assert.match(rows[3].textContent, /永久跳过（评分关闭）/);
  assert.match(rows[4].textContent, /待评估/);
  assert.match(rows[5].textContent, /API 未提供状态.*短时行为资格未提供/s);
  for (const row of rows.slice(2)) assert.doesNotMatch(row.textContent, /已评估|本事件增分|评估后分数/);
  assert.equal(p.$("event-table").querySelectorAll("script, img").length, 0);
});

await test("unrecorded users do not appear as fully evaluated zero risk", async () => {
  const p = await page();
  p.mock.get = (path, query) => path === "events"
    ? { page: 1, page_size: 50, total: 1, items: [group({ has_record: false, risk_score: 0 })] } : undefined;
  p.$("event-mode").value = "uid";
  await p.trigger("event-mode", "change");
  assert.match(p.$("event-table").textContent, /无风险档案（不表示已评估全部事件）/);
  assert.doesNotMatch(p.$("event-table").textContent, /0 分 · 正常/);
});

await test("module navigation supports manual keyboard activation and the mobile selector", async () => {
  const p = await page();
  const tabs = p.document.querySelectorAll(".tabs button");
  assert.equal(tabs[0].attributes["aria-selected"], "true");
  assert.equal(tabs[0].attributes["aria-controls"], "overview");
  assert(p.$("login").hidden);
  await tabs[0].emit("keydown", { key: "ArrowUp" });
  assert.equal(p.document.activeElement, tabs.at(-1));
  assert.equal(tabs.at(-1).tabIndex, 0);
  assert(!p.$("overview").hidden);
  await tabs.at(-1).emit("keydown", { key: "Home" });
  assert.equal(p.document.activeElement, tabs[0]);
  p.$("module-select").value = "logs";
  await p.trigger("module-select", "change");
  assert(!p.$("logs").hidden); assert(p.$("overview").hidden);
  assert.equal(tabs.find((button) => button.dataset.tab === "logs").attributes["aria-selected"], "true");
  assert(p.calls.some((call) => call.method === "SSE"));
});

await test("navigation failures persist for retry and late failures cannot replace another module", async () => {
  const p = await page(); const old = deferred();
  p.mock.get = (path) => path === "storage" ? old.promise : undefined;
  const first = p.tab("storage"); await flush();
  assert.equal(p.$("storage").attributes["aria-busy"], "true");
  await p.tab("login"); old.reject(new Error("旧请求失败")); await first;
  assert(p.$("panel-feedback").hidden);
  p.mock.get = (path) => { if (path === "storage") throw new Error("临时离线"); };
  await p.tab("storage");
  assert.match(p.$("panel-message").textContent, /临时离线/);
  assert(!p.$("retry-tab").hidden);
  p.mock.get = null; await p.trigger("retry-tab");
  assert(p.$("panel-feedback").hidden);
  assert.equal(p.$("storage").attributes["aria-busy"], "false");
});

await test("settings show unsaved state, lock the whole form during save and retain drafts on failure", async () => {
  const p = await page(); const pending = deferred();
  const name = p.$("config-form").querySelector('input[data-config-key="display_name"]');
  name.value = "未保存的名称"; await name.emit("input");
  assert(!p.$("config-unsaved").hidden);
  p.mock.post = (path) => path === "config/save" ? pending.promise : undefined;
  const save = p.trigger("save-config"); await flush();
  assert(p.$("config-form").disabled); assert(p.$("restore-defaults").disabled);
  assert.equal(p.$("save-config").attributes["aria-busy"], "true");
  pending.reject(new Error("保存失败")); await save;
  assert(!p.$("config-form").disabled); assert(!p.$("restore-defaults").disabled);
  assert.equal(name.value, "未保存的名称"); assert(p.app.state.configDirty);
  name.value = config.profiles[0].display_name; await name.emit("input");
  assert(p.$("config-unsaved").hidden);
});

await test("a cancelled unload leaves authentication, timers and future actions usable", async () => {
  const p = await page();
  const name = p.$("config-form").querySelector('input[data-config-key="display_name"]');
  name.value = "草稿"; await name.emit("input");
  let warned = false; p.lifecycle("beforeunload", { preventDefault() { warned = true; } });
  assert(warned); assert(!p.app.state.unloaded);
  await p.tab("login"); assert.match(p.$("login-facts").textContent, /logged_in/);
  await p.trigger("check-login"); assert.equal(p.posts().at(-1).path, "auth/check");
});

await test("source saves synchronize in place and preserve settings and publishing drafts", async () => {
  const p = await page(); await p.topics.loadSources();
  p.$("publish-text").value = "尚未发布的正文";
  const name = p.$("config-form").querySelector('input[data-config-key="display_name"]');
  name.value = "本地档案草稿"; await name.emit("input");
  const pc = p.$("fallback-source-list").querySelector('input[data-fallback-source="pc_game"]');
  pc.checked = true; await pc.emit("change");
  p.mock.post = () => ({ changed: ["proactive_feed"] });
  await p.topics.saveFallbackSources();
  assert(!p.mock.reloaded);
  assert.equal(p.$("publish-text").value, "尚未发布的正文");
  assert.equal(p.app.state.config.profiles[0].display_name, "本地档案草稿");
  assert.deepEqual(plain(p.app.state.config.proactive_feed.fallback_sources), ["pc_game"]);
  assert.deepEqual(plain(p.app.state.savedConfig.proactive_feed.fallback_sources), ["pc_game"]);
  assert(p.app.state.configDirty); assert(!p.$("save-fallback-sources").disabled);
  assert(p.$("fallback-source-list").querySelector('input[data-fallback-source="pc_game"]').checked);
  assert.equal(p.timers.size, 0);
  p.$("config-form").disabled = true;
  await p.topics.saveFallbackSources(); assert.equal(p.posts().length, 1);
});

await test("candidate refresh preserves other edited comments and confirms review without promising a send", async () => {
  const p = await page(); let items = [1, 2].map((id) => ({ id, profile_id: "second", post_id: String(id), status: "pending", generated_text: `原文 ${id}` }));
  p.mock.get = (path, query) => path === "feed/candidates" ? { items: query.status === "pending" ? items : [] } : undefined;
  p.mock.post = (path) => { if (path.endsWith("/reject")) { items = items.filter((item) => item.id !== 1); return {}; } };
  await p.tab("feed");
  let cards = p.$("candidate-list").children;
  const editor = cards[1].querySelector("textarea"); editor.value = "第二条手工修改"; await editor.emit("input");
  await cards[0].querySelectorAll("button")[1].emit("click");
  await p.tab("overview"); await p.tab("feed");
  cards = p.$("candidate-list").children;
  assert.equal(cards[0].querySelector("textarea").value, "第二条手工修改");
  const approve = cards[0].querySelector("button").emit("click"); await flush();
  assert.match(p.$("confirm-message").textContent, /第二条手工修改/);
  assert.match(p.$("confirm-message").textContent, /此步骤不会发送评论/);
  await p.trigger("confirm-cancel"); await approve;
  assert.equal(p.posts().length, 1);
});

await test("navigation preserves the source save lock until its transaction finishes", async () => {
  const p = await page(); await p.topics.loadSources(); const response = deferred();
  p.mock.post = (path) => path === "config/save" ? response.promise : undefined;
  const sourceSave = p.topics.saveFallbackSources(); await flush();
  assert.equal(p.$("sources").attributes["aria-busy"], "true");
  await p.tab("settings"); await p.tab("sources");
  assert.equal(p.$("sources").attributes["aria-busy"], "true");
  await p.trigger("save-config"); assert.equal(p.posts().length, 1);
  response.resolve({ changed: ["proactive_feed"] }); await sourceSave;
  assert.equal(p.$("sources").attributes["aria-busy"], "false");
});

await test("source changes persist in settings after a profile switch during the save response", async () => {
  const p = await page(); await p.topics.loadSources(); const response = deferred();
  const pc = p.$("fallback-source-list").querySelector('input[data-fallback-source="pc_game"]');
  pc.checked = true; await pc.emit("change");
  let saved = plain(config);
  p.mock.get = (path) => path === "config" ? plain(saved) : undefined;
  p.mock.post = async (path, body) => {
    if (path === "config/save") { await response.promise; saved = plain(body); return { changed: ["proactive_feed"] }; }
  };
  const sourceSave = p.topics.saveFallbackSources(); await flush();
  await p.tab("login"); p.$("login-profile").value = "second"; await p.trigger("login-profile", "change");
  response.resolve(); await sourceSave;
  assert.deepEqual(plain(p.app.state.config.proactive_feed.fallback_sources), ["pc_game"]);
  assert.deepEqual(plain(p.app.state.savedConfig.proactive_feed.fallback_sources), ["pc_game"]);
  await p.trigger("save-config");
  assert.deepEqual(p.posts().at(-1).body.proactive_feed.fallback_sources, ["pc_game"]);
});

await test("invalid numeric settings remain editable and saving opens their collapsed group", async () => {
  const p = await page();
  const control = p.$("config-form").querySelector('input[data-config-key="watch_score"]');
  let group = control.parentNode; while (group.tagName !== "details") group = group.parentNode;
  control.value = ""; await control.emit("input"); group.open = false;
  assert.equal(control.attributes["aria-invalid"], "true");
  await p.trigger("save-config"); assert(group.open); assert.equal(p.posts().length, 0);
  control.value = "20"; await control.emit("input");
  assert.equal(control.attributes["aria-invalid"], undefined); assert(control.checkValidity());
});

await test("publishing defaults update untouched fields and preserve independent user edits", async () => {
  const p = await page(); const updated = plain(config);
  updated.publishing.default_topic_ids = "50"; updated.publishing.view_limit = 3;
  p.app.publishing.configure(updated);
  assert.equal(p.$("post-topics").value, "50"); assert.equal(p.$("post-view-limit").value, "3");
  p.$("post-topics").value = "";
  updated.publishing.default_topic_ids = "60"; updated.publishing.view_limit = 2;
  p.app.publishing.configure(updated);
  assert.equal(p.$("post-topics").value, ""); assert.equal(p.$("post-view-limit").value, "2");
  p.$("post-view-limit").value = "1"; updated.publishing.view_limit = 3;
  p.app.publishing.configure(updated); assert.equal(p.$("post-view-limit").value, "1");
});

await test("confirmations isolate the background and cancel on Escape without posting", async () => {
  const p = await page(); await p.tab("login");
  p.$("logout").focus(); const logout = p.$("logout").emit("click"); await flush();
  assert(p.$("page-content").inert); assert.equal(p.document.activeElement, p.$("confirm-cancel"));
  for (const handler of p.document.listeners.keydown) handler({ key: "Escape" });
  await logout; assert(!p.$("page-content").inert); assert.equal(p.posts().length, 0);
  const frame = p.app.state.confirmFocusFrame; assert(frame); p.timers.get(frame).callback(); p.timers.delete(frame);
  assert.equal(p.document.activeElement, p.$("logout"));
});

console.log(`${passed} dashboard checks passed`);
