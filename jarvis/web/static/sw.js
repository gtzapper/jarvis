// Jarvis app shell. Live data always comes from the network (never cached: it's your lab's current state).
// The only things kept offline are the icon and a small "can't reach Jarvis" page.
const CACHE = "jarvis-v1";
const OFFLINE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1216;color:#e6e8eb;
font:15px system-ui,sans-serif;text-align:center;padding:24px}img{width:96px;height:96px;border-radius:22px}p{color:#98a2b3}
button{margin-top:14px;padding:10px 18px;border:0;border-radius:10px;background:#3b82f6;color:#fff;font:inherit}</style></head>
<body><div><img src="/icon-192.png" alt=""><h2>Can't reach Jarvis</h2><p>Are you on your home network (or VPN)?</p>
<button onclick="location.reload()">Try again</button></div></body></html>`;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(["/icon-192.png"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } })));
  } else if (new URL(req.url).pathname === "/icon-192.png") {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
  }
});
