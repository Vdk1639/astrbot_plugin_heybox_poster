// The host scopes relative API paths to astrbot_plugin_heybox_poster and supplies authentication.
const host = window.AstrBotPluginPage;
let readyPromise;
let closed = false;

function assertOpen() {
  if (closed) throw new Error("管理页面已关闭");
}

export const bridge = {
  async ready() {
    assertOpen();
    if (!host) throw new Error("请从 AstrBot WebUI 的插件可视化视图打开管理页");
    readyPromise ||= host.ready();
    await readyPromise;
    assertOpen();
  },
  async subscribeSSE(path, handlers) {
    await this.ready();
    return host.subscribeSSE(path, handlers);
  },
  unsubscribeSSE(id) {
    return host?.unsubscribeSSE(id);
  },
};

for (const method of ["apiGet", "apiPost"]) {
  bridge[method] = async (...args) => {
    await bridge.ready();
    const result = await host[method](...args);
    assertOpen();
    return result;
  };
}

window.addEventListener("pagehide", () => { closed = true; });
window.addEventListener("beforeunload", () => { closed = true; });
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});
