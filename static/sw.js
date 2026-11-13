const CACHE='tradingbot-shell-v2';
const SHELL=['/manifest.webmanifest','/static/icon-192.png','/static/icon-512.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('tradingbot-shell-')&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.origin!==location.origin || u.pathname.startsWith('/api/') || u.pathname.startsWith('/saxo/') || ['/login','/logout','/healthz'].includes(u.pathname) || e.request.method!=='GET') return;
  e.respondWith(fetch(e.request).then(r=>{if(r.ok&&!/no-store/i.test(r.headers.get('Cache-Control')||'')){const c=r.clone();caches.open(CACHE).then(x=>x.put(e.request,c));}return r}).catch(()=>caches.match(e.request).then(r=>r||caches.match('/manifest.webmanifest'))));
});

