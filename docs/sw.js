// 報告每天更新，所以 HTML 一律 network-first：有網路就拿最新的，
// 沒網路才回快取。反過來（cache-first）會讓使用者盯著昨天的報告卻毫無察覺 ——
// 對一份每日更新的財經報告來說，那比打不開更糟。
// 圖示與 manifest 幾乎不變，用 cache-first 省流量。

const CACHE = "tw-brief-v1";
const SHELL = [
  "./index.html",
  "./latest-postmarket.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
];

self.addEventListener("install", (e) => {
  // 個別 add，任何一項失敗（例如盤後報告還沒產生過）不影響其餘
  e.waitUntil(
    caches.open(CACHE).then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  const isHTML =
    req.mode === "navigate" ||
    (req.headers.get("accept") || "").includes("text/html");

  if (isHTML) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() =>
          caches.match(req).then((hit) => {
            if (hit) return hit;
            // 只有報告頁離線時才退回首頁。導師用的表單頁退回一份股市報告，
            // 比誠實說「離線」更容易讓人以為是網址打錯了。
            const p = new URL(req.url).pathname;
            const isReport =
              /\/(index\.html)?$/.test(p) ||
              /\/(pre|post)market-/.test(p) ||
              /latest-postmarket/.test(p);
            if (isReport) return caches.match("./index.html");
            return new Response(
              '<!doctype html><meta charset="utf-8"><title>目前離線</title>' +
                '<body style="font:16px/1.75 system-ui,sans-serif;padding:2rem;max-width:32rem;margin:auto">' +
                '<h1 style="font-size:1.2rem">目前離線</h1>' +
                "<p>這一頁還沒有離線副本，請連上網路後重新整理。</p>",
              { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
            );
          })
        )
    );
    return;
  }

  e.respondWith(
    caches.match(req).then(
      (hit) =>
        hit ||
        fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
    )
  );
});
