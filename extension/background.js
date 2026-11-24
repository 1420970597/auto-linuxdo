import connection from "./connection.js";

const FORUM = "https://linux.do/";
const ALARM = "sync-forum-session";
let running;

async function ensureAlarm() {
  if (!await chrome.alarms.get(ALARM)) {
    await chrome.alarms.create(ALARM, { periodInMinutes: 1 });
  }
}

async function synchronize() {
  let state;
  try {
    const cookie = await chrome.cookies.get({ url: FORUM, name: "_t" });
    const available = cookie && !cookie.partitionKey && cookie.path === "/" &&
      ["linux.do", ".linux.do"].includes(cookie.domain);
    const options = {
      method: available ? "POST" : "DELETE",
      headers: { Authorization: "Bearer " + connection.key },
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(7000),
    };
    if (available) {
      const { name, value, domain, path, expirationDate } = cookie;
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify([{ name, value, domain, path, expirationDate }]);
    }
    const response = await fetch(connection.url, options);
    if (response.ok) {
      state = (await response.json()).state;
    } else {
      state = response.status === 401 ? "invalid_key" : "sync_error";
    }
  } catch {
    state = "unreachable";
  }
  await chrome.storage.local.set({ remoteStatus: state });
  return state;
}

function sync() {
  running ??= synchronize().finally(() => { running = undefined; });
  return running;
}

async function autoSync() {
  const { remoteEnabled } = await chrome.storage.local.get("remoteEnabled");
  if (!remoteEnabled || !await chrome.permissions.contains({ origins: [FORUM + "*"] })) return;
  await ensureAlarm();
  await sync();
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM) void autoSync();
});
chrome.runtime.onStartup.addListener(autoSync);
chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.remove(["connection", "status"]);
  await autoSync();
});
chrome.cookies.onChanged.addListener(({ cookie, cause, removed }) => {
  if ((!removed || cause !== "overwrite") && cookie.name === "_t" &&
      ["linux.do", ".linux.do"].includes(cookie.domain)) {
    void autoSync();
  }
});

async function startSync() {
  await chrome.storage.local.set({ remoteEnabled: true, remoteStatus: "syncing" });
  await ensureAlarm();
  return await sync();
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.url !== chrome.runtime.getURL("sync.html") || message?.action !== "sync") return;
  startSync().then(respond, () => respond("unreachable"));
  return true;
});
