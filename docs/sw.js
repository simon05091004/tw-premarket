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
          caches.match(req).then((hit) => hit || caches.match("./index.html"))
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
