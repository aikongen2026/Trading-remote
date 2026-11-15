// Installation assets only. Never cache account pages or trading controls.
const CACHE='tradingbot-shell-v3';
const SHELL=['/manifest.webmanifest','/static/icon-192.png','/static/icon-512.png'];
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(
    keys.filter(key=>key.startsWith('tradingbot-shell-')&&key!==CACHE).map(key=>caches.delete(key))
  )).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',event=>{
  const req=event.request;
  const url=new URL(req.url);
  if(req.method!=='GET'||url.origin!==location.origin||!SHELL.includes(url.pathname))return;
  event.respondWith(fetch(req).catch(()=>caches.match(req)));
});
