const FORUM = "https://linux.do/";
const ALARM = "sync-forum-session";
const KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;
let operations = Promise.resolve();

function queue(operation) {
  const result = operations.then(operation);
  operations = result.catch(() => {});
  return result;
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}

async function verifyService(url, key) {
  const encoder = new TextEncoder();
  const challenge = hex(crypto.getRandomValues(new Uint8Array(16)));
  const hmac = await crypto.subtle.importKey(
    "raw", encoder.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const expected = hex(await crypto.subtle.sign("HMAC", hmac, encoder.encode(challenge)));
  const response = await fetch(url + "?challenge=" + challenge, {
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(6000),
  });
  return response.ok && (await response.json()).proof === expected;
}

async function sync() {
  const { connection } = await chrome.storage.local.get("connection");
  if (!connection) return;

  let state;
  try {
    const url = "http://127.0.0.1:" + connection.port + "/cookie-sync";
    if (!await verifyService(url, connection.key)) {
      await chrome.storage.local.set({ status: "invalid_key" });
      return;
    }
    const cookie = await chrome.cookies.get({ url: FORUM, name: "_t" });
    const available = cookie && !cookie.partitionKey && cookie.path === "/" &&
      ["linux.do", ".linux.do"].includes(cookie.domain);
    const options = {
      method: available ? "POST" : "DELETE",
      headers: { Authorization: "Bearer " + connection.key },
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(6000),
    };
    if (available) {
      const { name, value, domain, path, expirationDate } = cookie;
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify([{ name, value, domain, path, expirationDate }]);
    }
    const response = await fetch(url, options);
    if (response.ok) {
      state = (await response.json()).state;
    } else if (response.status === 401) {
      state = "invalid_key";
    } else {
      state = "sync_error";
    }
  } catch {
    state = "unreachable";
  }
  await chrome.storage.local.set({ status: state });
}

async function ensureAlarm() {
  if (!await chrome.alarms.get(ALARM)) {
    await chrome.alarms.create(ALARM, { periodInMinutes: 1 });
  }
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM) queue(sync);
});
chrome.runtime.onStartup.addListener(() => queue(async () => {
  const { connection } = await chrome.storage.local.get("connection");
  if (connection) {
    await ensureAlarm();
    await sync();
  }
}));
chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (!removed && cookie.name === "_t" && ["linux.do", ".linux.do"].includes(cookie.domain)) {
    queue(sync);
  }
});

async function handle(message) {
  if (message.action === "enable") {
    const { key, port } = message;
    if (typeof key !== "string" || !KEY_PATTERN.test(key) ||
        !Number.isInteger(port) || port < 1 || port > 65535) {
      return false;
    }
    await chrome.storage.local.set({ connection: { key, port }, status: "syncing" });
    await ensureAlarm();
    await sync();
  } else if (message.action === "sync") {
    await sync();
  } else if (message.action === "pause") {
    await chrome.alarms.clear(ALARM);
    await chrome.storage.local.remove(["connection", "status"]);
    await chrome.permissions.remove({ origins: ["https://linux.do/*"] });
  } else {
    return false;
  }
  return true;
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.url !== chrome.runtime.getURL("sync.html")) return;
  queue(() => handle(message)).then(reply, () => reply(false));
  return true;
});
