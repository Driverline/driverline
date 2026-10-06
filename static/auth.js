// Sends signed-out users to the login page, and shows the account/quota line when #me exists.
(function(){
  const f=window.fetch.bind(window);
  window.fetch=async function(...a){
    const r=await f(...a);
    if(r.status===401&&!location.pathname.startsWith('/login'))location.href='/login.html';
    return r;
  };
  window.dlMe=async function(){
    const e=document.getElementById('me');
    try{const r=await fetch('/api/auth/me');if(!r.ok)return;const u=await r.json();
      if(e)e.textContent=u.email+' \u00b7 '+u.tier+' \u00b7 AI analyses today '+u.used+'/'+u.limit;
      if(u.tier==='admin')adminTab()}catch(x){}
  };
  function tabs(){
    const h=document.querySelector('header');
    if(!h||document.querySelector('nav.tabs')||/^\/(login|reset)/.test(location.pathname))return;
    const here=location.pathname==='/index.html'?'/':location.pathname;
    const n=document.createElement('nav');n.className='tabs';
    [['Analysis','/'],['Risk','/risk.html'],['Journal','/journal.html'],['MT5','/mt5.html']].forEach(([t,u])=>{
      const a=document.createElement('a');a.href=u;a.textContent=t;if(u===here)a.className='on';n.append(a)});
    h.after(n);
  }
  function adminTab(){
    const n=document.querySelector('nav.tabs');if(!n||n.querySelector('[data-admin]'))return;
    const a=document.createElement('a');a.href='/admin.html';a.textContent='Admin';a.dataset.admin='1';
    if(location.pathname==='/admin.html')a.className='on';n.append(a);
  }
  document.addEventListener('DOMContentLoaded',()=>{
    tabs();
    window.dlMe();
    const o=document.getElementById('signout');
    if(o)o.onclick=async ev=>{ev.preventDefault();await f('/api/auth/logout',{method:'POST'});location.href='/login.html'};
  });
})();
