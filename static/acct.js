// Which of the member's MT5 accounts the app is acting on. One choice, remembered in this browser, shared by every page.
// With a single account nothing is shown and nothing changes. The server never guesses between several accounts for trades, closes
// or settings, so every action sends the chosen account's id.
(function(){
  const KEY='dl_acct';
  const get=()=>{try{const v=localStorage.getItem(KEY);return v?Number(v)||null:null}catch(e){return null}};
  const put=v=>{try{if(v)localStorage.setItem(KEY,String(v));else localStorage.removeItem(KEY)}catch(e){}};
  const A=window.dlAcct={
    id:get,
    set(v){put(v)},
    qs(first){const i=get();return i?(first===false?'&':'?')+'account='+i:''},          // '?account=2' (or '&account=2')
    body(o){const i=get();return i?Object.assign({},o,{account:i}):o},                  // adds the account to a request body
    tag(s){return s&&s.account?(s.label?s.label+' ':'')+'('+(s.account.demo?'DEMO':'LIVE')+')':''},   // "Live (LIVE)" style name for buttons
    // Status of the chosen account. A stored id that no longer exists (removed account) is dropped and the call repeated once.
    async status(){
      for(let n=0;n<2;n++){
        const r=await fetch('/api/ea/status'+A.qs()),d=await r.json().catch(()=>({}));
        if(r.status===404&&get()){put(null);continue}
        if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));
        const many=(d.accounts||[]).length>1;
        if(d.account_id&&(many?get()!==d.account_id:get()))put(many?d.account_id:null);       // follow what the server resolved; forget it for a single account
        return d}
    },
    async specs(){
      const r=await fetch('/api/ea/specs'+A.qs()),d=await r.json().catch(()=>[]);return r.ok&&Array.isArray(d)?d:[]},
    // A small "MT5 account" chooser. Returns null (nothing to show) unless the member has more than one account.
    picker(s,onchange){
      const list=(s&&s.accounts)||[];if(list.length<2)return null;
      const w=document.createElement('div');w.className='acctpick';w.style.cssText='display:flex;gap:8px;align-items:center;margin:0 0 10px';
      const l=document.createElement('span');l.className='mut';l.textContent='MT5 account';
      const sel=document.createElement('select');sel.style.cssText='flex:1;min-width:0';
      list.forEach(a=>{const o=document.createElement('option');o.value=a.id;
        o.textContent=a.label+(a.demo===true?' - DEMO':a.demo===false?' - LIVE':'')+(a.login?' ('+a.login+')':'')+(a.connected?'':' - offline');sel.append(o)});
      sel.value=String(s.account_id);
      sel.onchange=()=>{put(Number(sel.value));if(onchange)onchange(Number(sel.value))};
      w.append(l,sel);return w}
  };
})();
