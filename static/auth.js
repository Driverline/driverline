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
      if(e)e.textContent=u.email+' · '+u.tier+' · AI analyses today '+u.used+'/'+u.limit;
      if(u.tier==='admin')adminTab();
      if(u.rewards)rewardsTab();
      partnerTab();
      window.dlBadge()}catch(x){}
  };
  // Unread counters on the Messages tabs: the member's own (announcements + replies) and, for an admin, members' unread messages.
  function setBadge(a,n){
    if(!a)return;let b=a.querySelector('.dlb');
    if(!n){if(b)b.remove();return}
    if(!b){b=document.createElement('span');b.className='dlb';b.style.cssText='display:inline-block;background:var(--gold,#e8b84a);color:#02122c;border-radius:99px;font-size:11px;font-weight:700;padding:1px 6px;margin-left:6px';a.append(b)}
    b.textContent=n>99?'99+':String(n);
  }
  window.dlBadge=async function(){
    if(/^\/(login|reset)/.test(location.pathname))return;
    try{const r=await f('/api/inbox/summary');if(r.ok)setBadge(document.querySelector('nav.tabs a[href="/inbox.html"]'),(await r.json()).unread||0)}catch(x){}
    const ad=document.querySelector('nav.tabs a[href="/admin-messages.html"]');
    if(ad)try{const r=await f('/api/admin/inbox/summary');if(r.ok)setBadge(ad,(await r.json()).unread||0)}catch(x){}
  };
  function tabs(){
    const h=document.querySelector('header');
    if(!h||document.querySelector('nav.tabs')||/^\/(login|reset)/.test(location.pathname))return;
    const here=location.pathname==='/index.html'?'/':location.pathname;
    const n=document.createElement('nav');n.className='tabs';
    [['Home','/'],['Analysis','/analysis.html'],['Signals','/signals.html'],['Risk','/risk.html'],['Journal','/journal.html'],['MT5','/mt5.html'],['Billing','/billing.html'],['Messages','/inbox.html']].forEach(([t,u])=>{
      const a=document.createElement('a');a.href=u;a.textContent=t;if(u===here)a.className='on';n.append(a)});
    h.after(n);
  }
  function rewardsTab(){
    const n=document.querySelector('nav.tabs');if(!n||n.querySelector('[data-rewards]'))return;
    const a=document.createElement('a');a.href='/rewards.html';a.textContent='Rewards';a.dataset.rewards='1';
    if(location.pathname==='/rewards.html')a.className='on';
    const m=n.querySelector('a[href="/inbox.html"]');if(m)m.after(a);else n.append(a);
  }
  async function partnerTab(){          // the Partner tab only appears for people who may use the programme (admins, reviewers, or everyone once public)
    try{const r=await f('/api/partners/access');if(!r.ok||!(await r.json()).access)return;
      const n=document.querySelector('nav.tabs');if(!n||n.querySelector('[data-partner]'))return;
      const a=document.createElement('a');a.href='/partner.html';a.textContent='Partner';a.dataset.partner='1';
      if(location.pathname==='/partner.html')a.className='on';
      const m=n.querySelector('[data-rewards]')||n.querySelector('a[href="/inbox.html"]');if(m)m.after(a);else n.append(a)}catch(x){}
  }
  function adminTab(){
    const n=document.querySelector('nav.tabs');if(!n||n.querySelector('[data-admin]'))return;
    const a=document.createElement('a');a.href='/admin.html';a.textContent='Admin';a.dataset.admin='1';
    if(location.pathname==='/admin.html')a.className='on';n.append(a);
    const m=document.createElement('a');m.href='/admin-messages.html';m.textContent='Inbox';m.dataset.admin='1';
    if(location.pathname==='/admin-messages.html')m.className='on';n.append(m);
    const p=document.createElement('a');p.href='/admin-payments.html';p.textContent='Payments';p.dataset.admin='1';
    if(location.pathname==='/admin-payments.html')p.className='on';n.append(p);
    const q=document.createElement('a');q.href='/admin-partners.html';q.textContent='Partners';q.dataset.admin='1';
    if(location.pathname==='/admin-partners.html')q.className='on';n.append(q);
    const w=document.createElement('a');w.href='/admin-rewards.html';w.textContent='Rewards';w.dataset.admin='1';
    if(location.pathname==='/admin-rewards.html')w.className='on';n.append(w);
  }
  document.addEventListener('DOMContentLoaded',()=>{
    tabs();
    window.dlMe();
    const o=document.getElementById('signout');
    if(o)o.onclick=async ev=>{ev.preventDefault();await f('/api/auth/logout',{method:'POST'});location.href='/login.html'};
  });
})();
