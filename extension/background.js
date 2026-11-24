import connection from "./connection.js";
import { readForumPage } from "./capture.js";

async function capture(tabId) {
  if (!Number.isInteger(tabId)) return "unsupported_page";
  let result;
  try {
    [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: readForumPage,
    });
  } catch {
    return "unsupported_page";
  }
  if (!result?.page) return result?.state || "page_empty";

  try {
    const response = await fetch(connection.url, {
      method: "POST",
      headers: { Authorization: "Bearer " + connection.key, "Content-Type": "application/json" },
      body: JSON.stringify(result.page),
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(10_000),
    });
    if (response.ok) return "captured";
    if (response.status === 401) return "invalid_key";
    return "capture_error";
  } catch {
    return "unreachable";
  }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.url !== chrome.runtime.getURL("sync.html") || message?.action !== "capture") return;
  capture(message.tabId).then(respond, () => respond("capture_error"));
  return true;
});
