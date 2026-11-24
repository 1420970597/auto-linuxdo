const status = document.querySelector("#status");
const button = document.querySelector("#sync");
const messages = {
  syncing: "正在同步…",
  pending: "已同步，服务器正在验证会话。",
  authenticated: "会话已同步，服务器已确认登录。",
  signed_out: "请先在当前 Chrome 登录 Linux.do。",
  cookies_expired: "论坛会话已失效，请重新登录再同步。",
  manual_verification: "会话已送达。服务器访问论坛时遇到人工验证。",
  site_blocked: "会话已送达。论坛拒绝了服务器读取。",
  rate_limited: "会话已送达。论坛暂时限流。",
  invalid_key: "连接码已失效，请重新获取插件。",
  sync_error: "服务器拒绝了这次同步。",
  unreachable: "无法连接同步服务器。",
};

function show(state) {
  if (state) status.textContent = messages[state] || "已发送会话，可在服务器检查读取状态。";
}

chrome.storage.local.get("remoteStatus").then(({ remoteStatus }) => show(remoteStatus));
chrome.storage.onChanged.addListener(changes => show(changes.remoteStatus?.newValue));
button.addEventListener("click", async () => {
  button.disabled = true;
  try {
    if (!await chrome.permissions.request({ origins: ["https://linux.do/*"] })) {
      status.textContent = "请允许读取 Linux.do 会话。";
      return;
    }
    show("syncing");
    show(await chrome.runtime.sendMessage({ action: "sync" }));
  } catch {
    show("unreachable");
  } finally {
    button.disabled = false;
  }
});
