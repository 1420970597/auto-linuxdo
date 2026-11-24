const status = document.querySelector("#status");
const button = document.querySelector("#sync");
const messages = {
  captured: "已保存当前页面。可在 MCP 中查询。",
  manual_verification: "请在当前页面完成论坛验证，再点击采集。",
  unsupported_page: "请打开 Linux.do 或 V2EX 的主题、列表或搜索结果页。",
  page_empty: "当前页没有可采集的文本。请等页面加载后重试。",
  invalid_key: "连接码已失效，请重新获取插件。",
  capture_error: "服务器未能保存当前页面。",
  unreachable: "无法连接采集服务器。",
};

button.addEventListener("click", async () => {
  button.disabled = true;
  status.textContent = "正在采集…";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const result = await chrome.runtime.sendMessage({ action: "capture", tabId: tab?.id });
    status.textContent = messages[result] || messages.capture_error;
  } catch {
    status.textContent = messages.unreachable;
  } finally {
    button.disabled = false;
  }
});
