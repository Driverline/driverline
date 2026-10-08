
self.addEventListener('push',e=>{
  let d={};try{d=e.data.json()}catch(x){}
  e.waitUntil(self.registration.showNotification(d.title||'TradeLens',{body:d.body||'',tag:d.tag,data:{url:d.url||'/signals.html'},icon:'/icon-192.png',badge:'/favicon-48.png'}));
});
self.addEventListener('notificationclick',e=>{
  e.notification.close();const url=(e.notification.data&&e.notification.data.url)||'/signals.html';
  e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(l=>{
    for(const c of l){if('focus' in c){c.navigate(url);return c.focus()}}
    return clients.openWindow(url);
  }));
});
