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
      autoTab();
      window.dlWallet();
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
    if(ad)try{const r=await f('/api/admin/inbox/summary');if(r.ok){const j=await r.json();setBadge(ad,(j.unread||0)+(j.alerts||0))}}catch(x){}
  };
  function tabs(){
    const h=document.querySelector('header');
    if(!h||document.querySelector('nav.tabs')||/^\/(login|reset)/.test(location.pathname))return;
    const here=location.pathname==='/index.html'?'/':location.pathname;
    const n=document.createElement('nav');n.className='tabs';
    [['Home','/'],['Analysis','/analysis.html'],['Signals','/signals.html'],['Alerts','/alerts.html'],['Risk','/risk.html'],['Journal','/journal.html'],['MT5','/mt5.html'],['Billing','/billing.html'],['Messages','/inbox.html']].forEach(([t,u])=>{
      const a=document.createElement('a');a.href=u;a.textContent=t;if(u===here)a.className='on';n.append(a)});
    const w=document.createElement('div');w.className='dltop';h.before(w);w.append(h,n);   // logo and tabs stay together at the top while the page scrolls
  }
  // Wallet balance on the header's right, with a hide/reveal eye. Shown only where the server says the member has a wallet. The hidden state is a
  // per-device preference (localStorage, wrapped because it can be blocked).
  const EYE='<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  const EYE_OFF='<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.9 10.9 0 0 1 12 19c-7 0-11-7-11-7a19.8 19.8 0 0 1 5.06-5.94M9.9 4.24A10.9 10.9 0 0 1 12 4c7 0 11 7 11 7a19.9 19.9 0 0 1-3.17 4.19M14.12 14.12a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>';
  let walletUsd=null;
  const hidden=()=>{try{return localStorage.getItem('dl_wallet_hidden')==='1'}catch(x){return false}};
  function drawWallet(){
    const h=document.querySelector('header'),c=h&&h.querySelector('.dlwallet');if(!c||walletUsd==null)return;
    const hide=hidden();
    c.querySelector('.amt').textContent=hide?'\u2022\u2022\u2022\u2022':'$'+walletUsd.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    const b=c.querySelector('button');b.innerHTML=hide?EYE_OFF:EYE;b.setAttribute('aria-label',hide?'Show wallet balance':'Hide wallet balance');b.setAttribute('aria-pressed',hide?'true':'false');
  }
  window.dlWallet=async function(){
    const h=document.querySelector('header');
    if(!h||/^\/(login|reset)/.test(location.pathname))return;
    try{const r=await f('/api/wallet');if(!r.ok)return;const d=await r.json();
      let c=h.querySelector('.dlwallet');
      if(!d.enabled){if(c)c.remove();return}
      walletUsd=Number(d.balance_usd)||0;
      if(!c){
        c=document.createElement('div');c.className='dlwallet';
        const a=document.createElement('a');a.href='/billing.html#wallet';a.title='Open your wallet';
        const l=document.createElement('small');l.textContent='Wallet';const m=document.createElement('b');m.className='amt';a.append(l,m);
        const b=document.createElement('button');b.type='button';
        b.onclick=()=>{try{localStorage.setItem('dl_wallet_hidden',hidden()?'0':'1')}catch(x){}drawWallet()};
        c.append(a,b);h.append(c);
      }
      drawWallet();
    }catch(x){}
  };
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
  async function autoTab(){             // AutoTrading is private for now: the tab appears only where the server says it is available
    try{const r=await f('/api/auto/me');if(!r.ok||!(await r.json()).available)return;
      const n=document.querySelector('nav.tabs');if(!n||n.querySelector('[data-auto]'))return;
      const a=document.createElement('a');a.href='/auto.html';a.textContent='AutoTrading';a.dataset.auto='1';
      if(location.pathname==='/auto.html')a.className='on';
      const m=n.querySelector('a[href="/mt5.html"]');if(m)m.after(a);else n.append(a)}catch(x){}
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
  // The tab bar stays at the top while the page scrolls (see theme.css). Keep the current page's tab in view inside the bar.
  function showActiveTab(){
    const n=document.querySelector('nav.tabs'),a=n&&n.querySelector('a.on');
    if(a&&n.scrollWidth>n.clientWidth)n.scrollLeft=a.offsetLeft-(n.clientWidth-a.offsetWidth)/2;
  }
  document.addEventListener('DOMContentLoaded',()=>{
    tabs();
    [50,800,2500].forEach(ms=>setTimeout(showActiveTab,ms));       // later tabs (Admin, Partner, Auto) are added after sign-in is checked
    window.dlMe();
    const o=document.getElementById('signout');
    if(o)o.onclick=async ev=>{ev.preventDefault();await f('/api/auth/logout',{method:'POST'});location.href='/login.html'};
  });
})();
