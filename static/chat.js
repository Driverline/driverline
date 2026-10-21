/* TradeLens Help: the AI chat panel. Self-contained, no libraries. window.dlChat.open(context, firstQuestion) opens it. */
(function(){
  if(window.dlChat||!document.body)return;
  var KEY='dl_chat_v1',CSS=''
  +'#dlc-fab{position:fixed;right:14px;bottom:16px;z-index:9998;border:1px solid #1f4a8a;background:#0b1d44;color:#e9eefc;border-radius:999px;padding:10px 16px;font:600 14px/1 inherit;box-shadow:0 6px 20px rgba(0,0,0,.45);cursor:pointer;width:auto}'
  +'#dlc-fab span{color:#00c2ff}#dlc-fab.min{padding:0;width:44px;height:44px;border-radius:50%;font-size:20px;text-align:center;opacity:.92}#dlc-fab.min b{display:none}'
  +'#dlc-p{position:fixed;right:0;bottom:0;z-index:9999;width:min(100vw,430px);height:min(82vh,640px);display:none;flex-direction:column;background:#07102a;border:1px solid #16305f;border-radius:16px 16px 0 0;box-shadow:0 -8px 30px rgba(0,0,0,.55);color:#e9eefc;font:15px/1.45 inherit}'
  +'#dlc-p.on{display:flex}'
  +'#dlc-h{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid #16305f}'
  +'#dlc-h b{letter-spacing:.04em}#dlc-h small{display:block;color:#8d9bc4;font-size:12px;font-weight:400}'
  +'#dlc-x{background:none;border:0;color:#9fb0d9;font-size:22px;line-height:1;cursor:pointer;width:auto;padding:4px 8px}'
  +'#dlc-m{flex:1;overflow:auto;padding:12px 14px}'
  +'.dlc-b{max-width:92%;margin:8px 0;padding:9px 12px;border-radius:12px;word-wrap:break-word}'
  +'.dlc-u{margin-left:auto;background:#12356b;border:1px solid #1f4a8a}.dlc-a{background:#0b1a3b;border:1px solid #16305f}'
  +'.dlc-a ul{margin:6px 0;padding-left:20px}.dlc-note{color:#8d9bc4;font-size:12.5px;margin:6px 0}'
  +'#dlc-c{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}'
  +'#dlc-c button{width:auto;background:#0b1a3b;border:1px solid #1f4a8a;color:#cfe8ff;border-radius:999px;padding:7px 12px;font-size:13px;cursor:pointer}'
  +'#dlc-f{border-top:1px solid #16305f;padding:10px 12px}#dlc-r{display:flex;gap:8px;align-items:flex-end}'
  +'#dlc-i{flex:1;resize:none;background:#050b1f;color:#e9eefc;border:1px solid #1f4a8a;border-radius:12px;padding:9px 11px;font:inherit;font-size:15px;max-height:96px;margin:0}'
  +'#dlc-s{width:auto;padding:10px 16px;border-radius:12px;border:0;background:#00a9e0;color:#04101f;font-weight:700;cursor:pointer}#dlc-s[disabled]{opacity:.5}'
  +'#dlc-t{color:#8d9bc4;font-size:11.5px;margin-top:6px}';
  var st=document.createElement('style');st.textContent=CSS;document.head.appendChild(st);
  var state={msgs:[],ctx:null,left:null,busy:false};
  try{var s=JSON.parse(sessionStorage.getItem(KEY)||'null');if(s&&Array.isArray(s.msgs)){state.msgs=s.msgs.slice(-20);state.ctx=s.ctx||null}}catch(e){}
  function save(){try{sessionStorage.setItem(KEY,JSON.stringify({msgs:state.msgs.slice(-20),ctx:state.ctx}))}catch(e){}}
  function esc(t){return String(t).replace(/[&<>"]/g,function(m){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]})}
  function fmt(t){var out=[],list=false;esc(t).split('\n').forEach(function(l){
    var li=/^\s*[-*\u2022]\s+(.*)$/.exec(l),n=/^\s*\d+[.)]\s+(.*)$/.exec(l);
    if(li||n){if(!list){out.push('<ul>');list=true}out.push('<li>'+(li?li[1]:n[1])+'</li>')}
    else{if(list){out.push('</ul>');list=false}out.push(l.trim()?'<div>'+l+'</div>':'<div style="height:6px"></div>')}});
    if(list)out.push('</ul>');return out.join('').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')}
  var fab=document.createElement('button');fab.id='dlc-fab';fab.type='button';fab.innerHTML='<span>?</span><b>&nbsp; Ask TradeLens</b>';fab.setAttribute('aria-label','Ask TradeLens, the help chat');
  var p=document.createElement('div');p.id='dlc-p';p.setAttribute('role','dialog');p.setAttribute('aria-label','TradeLens help chat');
  p.innerHTML='<div id="dlc-h"><div><b>Ask TradeLens</b><small>Help with the app, analysis and trading basics</small></div><button id="dlc-x" type="button" aria-label="Close">\u00d7</button></div>'
   +'<div id="dlc-m"></div><div id="dlc-f"><div id="dlc-r"><textarea id="dlc-i" rows="1" maxlength="1000" placeholder="Ask a question..."></textarea><button id="dlc-s" type="button">Send</button></div>'
   +'<div id="dlc-t">AI can make mistakes. Education only, not financial advice.<span id="dlc-q"></span></div></div>';
  document.body.appendChild(fab);document.body.appendChild(p);setTimeout(function(){fab.classList.add('min')},5000);
  var m=p.querySelector('#dlc-m'),inp=p.querySelector('#dlc-i'),snd=p.querySelector('#dlc-s'),q=p.querySelector('#dlc-q');
  var CHIPS=['What does NO TRADE mean?','How do I read a Trade Card?','What is a stop loss?','How does my daily limit work?','What is a Boom index?'];
  function bubble(role,text){var d=document.createElement('div');d.className='dlc-b '+(role==='user'?'dlc-u':'dlc-a');d.innerHTML=role==='user'?esc(text):fmt(text);m.appendChild(d);m.scrollTop=m.scrollHeight;return d}
  function note(t){var d=document.createElement('div');d.className='dlc-note';d.textContent=t;m.appendChild(d);m.scrollTop=m.scrollHeight}
  function chips(list){var c=document.createElement('div');c.id='dlc-c';list.forEach(function(t){var b=document.createElement('button');b.type='button';b.textContent=t;b.onclick=function(){ask(t)};c.appendChild(b)});m.appendChild(c)}
  function draw(){m.textContent='';if(!state.msgs.length){bubble('assistant','Hi! I can explain how TradeLens works, what a Trade Card means, and the basics of trading synthetic indices. I can\u2019t tell you what to trade, but I can help you understand what you are looking at.');
      chips(state.ctx?['Explain this card in simple words','Why is this a NO TRADE?','What should I wait for?'].concat(CHIPS.slice(0,2)):CHIPS)}
    state.msgs.forEach(function(x){bubble(x.role,x.content)});
    if(state.ctx&&state.ctx.name)note('Chatting about the card for '+state.ctx.name+'.');
    q.textContent=state.left===null?'':'  \u00b7  '+state.left+' question'+(state.left===1?'':'s')+' left today'}
  function setBusy(b){state.busy=b;snd.disabled=b;inp.disabled=b}
  function ask(text){text=(text||inp.value).trim();if(!text||state.busy)return;inp.value='';inp.style.height='auto';
    var first=!state.msgs.length;state.msgs.push({role:'user',content:text});if(first)m.textContent='';bubble('user',text);var wait=bubble('assistant','Thinking...');setBusy(true);save();
    fetch('/api/chat',{method:'POST',credentials:'same-origin',headers:{'content-type':'application/json'},body:JSON.stringify({messages:state.msgs.slice(-8),card:state.ctx||undefined})})
    .then(function(r){return r.json().catch(function(){return{}}).then(function(d){return{ok:r.ok,status:r.status,d:d}})})
    .then(function(x){
      if(x.ok&&x.d.reply){state.msgs.push({role:'assistant',content:x.d.reply});state.left=x.d.left;wait.innerHTML=fmt(x.d.reply);
        q.textContent=state.left===null?'':'  \u00b7  '+state.left+' question'+(state.left===1?'':'s')+' left today'}
      else{state.msgs.pop();var msg=x.status===401?'Please sign in again to use the help chat.':(x.d.detail||'The help chat could not answer just now. Please try again.');
        wait.className='dlc-b dlc-a';wait.innerHTML=esc(msg)+(x.status===401?' <a href="/login.html" style="color:#5cc8ff">Sign in</a>':'')}
      save()})
    .catch(function(){state.msgs.pop();wait.textContent='Could not reach TradeLens. Check your connection and try again.';save()})
    .then(function(){setBusy(false);m.scrollTop=m.scrollHeight;if(p.classList.contains('on'))inp.focus()})}
  function open(ctx,first){if(ctx){var same=state.ctx&&JSON.stringify(state.ctx).slice(0,300)===JSON.stringify(ctx).slice(0,300);
      if(!same){state.ctx=ctx;state.msgs=[]}}p.classList.add('on');fab.style.display='none';draw();save();if(first&&!state.busy)ask(first);else setTimeout(function(){inp.focus()},50)}
  function close(){p.classList.remove('on');fab.style.display=''}
  fab.onclick=function(){open()};p.querySelector('#dlc-x').onclick=close;snd.onclick=function(){ask()};
  inp.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask()}});
  inp.addEventListener('input',function(){inp.style.height='auto';inp.style.height=Math.min(96,inp.scrollHeight)+'px'});
  window.dlChat={open:open,close:close}
})();
