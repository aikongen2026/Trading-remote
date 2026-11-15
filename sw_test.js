// Exercise the service worker fetch policy without a browser or network.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const handlers={};
const writes=[];
const deleted=[];
const cache={put:async(req)=>writes.push(req.url),addAll:async()=>{}};
let response;
const context={URL,location:{origin:'https://trading-remote.onrender.com'},
  self:{addEventListener:(name,fn)=>handlers[name]=fn,skipWaiting:async()=>{},clients:{claim:async()=>{}}},
  caches:{open:async()=>cache,keys:async()=>['tradingbot-shell-v1','tradingbot-shell-v2','other-app'],delete:async key=>deleted.push(key),match:async()=>null},
  fetch:async()=>response};
const script=fs.existsSync('static/sw.js')?'static/sw.js':'sw.js';
vm.runInNewContext(fs.readFileSync(script,'utf8'),context);
async function run(){
  let activation;
  handlers.activate({waitUntil:p=>activation=p});
  await activation;
  assert.deepEqual(deleted,['tradingbot-shell-v1','tradingbot-shell-v2']);
  for(const path of ['/saxo/oauth','/saxo/oauth/start','/saxo/oauth/callback?code=fake','/saxo/oauth/result','/login','/logout','/api/status']){
    let intercepted=false;
    handlers.fetch({request:{url:context.location.origin+path,method:'GET'},respondWith:()=>intercepted=true});
    assert.equal(intercepted,false,path+' must bypass the cache');
  }
  // Mobile PWA only caches static install resources. Never cache dashboard,
  // HTML, or arbitrary same-origin assets, even if they appear cacheable.
  for(const path of ['/', '/static/test.png']){
    let intercepted=false;
    handlers.fetch({request:{url:context.location.origin+path,method:'GET'},respondWith:()=>intercepted=true});
    assert.equal(intercepted,false,path+' must bypass app shell caching');
  }
  for(const path of ['/manifest.webmanifest','/static/icon-192.png','/static/icon-512.png']){
    response={ok:true,headers:{get:()=>null}};
    let completion;
    handlers.fetch({request:{url:context.location.origin+path,method:'GET'},respondWith:p=>completion=p});
    assert.ok(completion,path+' must be available for installation');
    await completion;
  }
  assert.equal(writes.length,0,'No authenticated pages or responses are cached');
  console.log('Service worker auth/cache guards passed');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
