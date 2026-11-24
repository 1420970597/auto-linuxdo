export function readForumPage() {
  const { hostname, pathname, protocol, port, href } = location;
  const linuxdo = hostname === "linux.do";
  if (protocol !== "https:" || port || (!linuxdo && !["v2ex.com", "www.v2ex.com"].includes(hostname))) {
    return { state: "unsupported_page" };
  }

  const visible = element => element.checkVisibility({ opacityProperty: true, visibilityProperty: true });
  const all = (selector, root = document) => [...root.querySelectorAll(selector)].filter(visible);
  const first = (selector, root) => all(selector, root)[0];
  const text = (element, limit) => element?.innerText?.trim().slice(0, limit) || "";

  if (all('#challenge-form, #challenge-stage, #cf-challenge-running, iframe[src*="challenges.cloudflare.com"], img[alt="CAPTCHA"]').length ||
      /^just a moment\b/i.test(document.title)) {
    return { state: "manual_verification" };
  }

  const path = pathname.replace(/\/+$/, "") || "/";
  const topicPath = linuxdo ? /^\/t\/(?:[\w%-]+\/)?[1-9]\d*(?:\/[1-9]\d*)?$/ : /^\/t\/[1-9]\d*$/;
  const listPath = linuxdo
    ? /^\/(?:latest|top|new|unread|categories|search|hot|c\/[\w%/-]+)?$/
    : /^\/(?:recent|go\/[\w-]+)?$/;

  function canonical(value) {
    const url = new URL(value);
    const params = new URLSearchParams();
    for (const name of ["page", "p"]) {
      const value = url.searchParams.get(name);
      if (/^[1-9][0-9]{0,5}$/.test(value || "")) params.append(name, value);
    }
    if (linuxdo && url.pathname === "/search" && url.searchParams.has("q")) {
      params.append("q", url.searchParams.get("q").slice(0, 160));
    }
    if (!linuxdo && url.pathname === "/" && url.searchParams.has("tab")) {
      params.append("tab", url.searchParams.get("tab").slice(0, 160));
    }
    url.pathname = url.pathname.replace(/\/+$/, "") || "/";
    url.search = params.toString();
    url.hash = "";
    return url.href;
  }

  if (topicPath.test(path)) {
    const titleSelector = linuxdo ? "h1 .fancy-title, #topic-title h1, h1[data-topic-id]" : "#Main .header h1, #Main h1";
    const title = text(first(titleSelector), 300);
    const posts = [];
    const body = linuxdo ? "" : text(first("#Main .topic_content"), 6000);
    if (linuxdo) {
      for (const article of all("article[data-post-id]")) {
        const number = Number(article.id.match(/^post[_-]([1-9]\d*)$/)?.[1]);
        if (!Number.isSafeInteger(number) || number > 2147483647) continue;
        const content = text(all(".cooked", article).find(el => el.closest("article") === article), 6000);
        if (content && posts.length < 8) {
          posts.push({ number, text: content });
        }
      }
    } else {
      for (const element of all('#Main [id^="r_"] .reply_content').slice(0, 8)) {
        const reply = element.closest('[id^="r_"]');
        const numberText = reply.querySelector(".no")?.innerText.trim();
        const number = /^[1-9]\d*$/.test(numberText || "") ? Number(numberText) : null;
        const content = text(element, 6000);
        if (content) posts.push({ number: Number.isSafeInteger(number) && number <= 2147483647 ? number : null, text: content });
      }
    }
    if (!title || (!body && !posts.length)) return { state: "page_empty" };
    return { page: { url: canonical(href), title, body, posts } };
  }

  if (!listPath.test(path)) return { state: "unsupported_page" };
  const selector = linuxdo
    ? ".topic-list-item .main-link a.raw-topic-link, .topic-list-item .main-link a.title, .fps-result a.search-link, .search-results a.search-link"
    : "#Main .item_title a[href]";
  const links = new Map();
  for (const link of all(selector)) {
    let url;
    try { url = new URL(link.href); } catch { continue; }
    const forumLink = linuxdo ? url.hostname === "linux.do" : ["v2ex.com", "www.v2ex.com"].includes(url.hostname);
    if (!forumLink || url.protocol !== "https:" || url.port || url.username || url.password ||
        !topicPath.test(url.pathname.replace(/\/+$/, ""))) continue;
    const destination = canonical(url.href);
    const title = text(link, 300);
    if (title && !links.has(destination)) links.set(destination, { title, url: destination });
    if (links.size === 25) break;
  }
  if (!links.size) return { state: "page_empty" };
  return { page: { url: canonical(href), title: document.title.trim().slice(0, 300) || hostname, links: [...links.values()] } };
}
