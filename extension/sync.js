const status = document.querySelector("#status");
const form = document.querySelector("#connect");
const actions = document.querySelector("#actions");
const key = document.querySelector("#key");
const port = document.querySelector("#port");
const buttons = [...document.querySelectorAll("button")];
const messages = {
  syncing: "正在同步当前 Chrome 的会话…",
  pending: "Cookie 已同步，Docker 正在验证会话。",
  authenticated: "Docker 已确认论坛会话。",
  signed_out: "请先在当前 Chrome 登录 Linux.do。",
  cookies_expired: "Docker 中的论坛会话已失效。请在当前 Chrome 重新登录论坛。",
  manual_verification: "Docker 访问论坛时遇到人工验证，读取已暂停。",
  site_blocked: "论坛拒绝了 Docker 的访问，读取已暂停。",
  rate_limited: "论坛暂时限流。",
  invalid_key: "同步密钥无效。请解除配对，再用 Docker 提供的同步密钥配对。",
  sync_error: "Docker 拒绝了这次同步。",
  unreachable: "无法连接 Docker 服务或读取论坛 Cookie。",
  cookies_invalid: "Docker 中的 Cookie 配置无效。",
  browser_unavailable: "Docker 中的 Chrome 沙箱暂未就绪。",
};

async function render() {
  const saved = await chrome.storage.local.get(["connection", "status"]);
  form.hidden = Boolean(saved.connection);
  actions.hidden = !saved.connection;
  status.textContent = saved.connection
    ? (messages[saved.status] || "Cookie 已同步。可通过 MCP 查看论坛会话状态。")
    : "在当前 Chrome 登录 Linux.do，然后配对本机 Docker 服务。";
}

async function send(message) {
  for (const button of buttons) button.disabled = true;
  try {
    const success = await chrome.runtime.sendMessage(message);
    if (!success) throw new Error("Sync action failed");
    await render();
  } catch {
    status.textContent = "扩展未能完成操作，请重试。";
  } finally {
    for (const button of buttons) button.disabled = false;
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (!await chrome.permissions.request({ origins: ["https://linux.do/*"] })) {
    status.textContent = "开启同步需要授权访问 Linux.do。";
    return;
  }
  const message = { action: "enable", key: key.value.trim(), port: port.valueAsNumber };
  key.value = "";
  await send(message);
});
document.querySelector("#sync").addEventListener("click", () => send({ action: "sync" }));
document.querySelector("#pause").addEventListener("click", () => send({ action: "pause" }));
chrome.storage.onChanged.addListener(() => render());
render();
