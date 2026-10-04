// Sends signed-out users to the login page, and shows the account/quota line when #me exists.
(function(){
  const f=window.fetch.bind(window);
  window.fetch=async function(...a){
    const r=await f(...a);
    if(r.status===401&&!location.pathname.startsWith('/login'))location.href='/login.html';
    return r;
  };
  window.dlMe=async function(){
    const e=document.getElementById('me');if(!e)return;
    try{const r=await fetch('/api/auth/me');if(!r.ok)return;const u=await r.json();
      e.textContent=u.email+' \u00b7 '+u.tier+' \u00b7 AI analyses today '+u.used+'/'+u.limit;
      const al=document.getElementById('adminlink');if(al&&u.tier==='admin')al.hidden=false}catch(x){}
  };
  document.addEventListener('DOMContentLoaded',()=>{
    window.dlMe();
    const o=document.getElementById('signout');
    if(o)o.onclick=async ev=>{ev.preventDefault();await f('/api/auth/logout',{method:'POST'});location.href='/login.html'};
  });
})();
