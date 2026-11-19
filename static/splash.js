/* TradeLens launch animation: the eye logo glows in, the name and "See the market clearly" follow, then the app opens.
   Shown once each time the app is opened (once per browser session), on the pages people land on (Home and Login). Tap anywhere to skip.
   Self-contained: it adds its own styles and element, never blocks the page (everything loads underneath), and removes itself when done.
   A person who asked their device to reduce motion gets a short, still fade instead. */
(function(){
  try{if(sessionStorage.getItem('tl_splash')==='1')return;sessionStorage.setItem('tl_splash','1')}catch(e){}
  var reduce=false;try{reduce=matchMedia('(prefers-reduced-motion: reduce)').matches}catch(e){}
  var css=
  '#tlsplash{position:fixed;inset:0;z-index:99999;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:0;padding:24px;text-align:center;'+
  'background:radial-gradient(90% 55% at 50% 38%,#0b2a66 0,#06143a 45%,#040a1b 80%);color:#f2f7ff;font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;'+
  'opacity:1;transition:opacity .55s ease;touch-action:manipulation;cursor:pointer}'+
  '#tlsplash.out{opacity:0;pointer-events:none}'+
  '#tlsplash .tle{width:min(62vw,300px);height:auto;opacity:0;transform:scale(.82);filter:drop-shadow(0 0 0 rgba(26,168,255,0));animation:tlEye 1.1s cubic-bezier(.2,.7,.2,1) .15s forwards,tlGlow 2.6s ease-in-out 1s infinite}'+
  '#tlsplash .tln{margin-top:14px;font:700 clamp(34px,10vw,50px)/1.05 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;letter-spacing:-.01em;opacity:0;transform:translateY(12px);animation:tlUp .7s ease .85s forwards}'+
  '#tlsplash .tln i{font-style:normal;background:linear-gradient(135deg,#04c8fb,#0187fe);-webkit-background-clip:text;background-clip:text;color:transparent}'+
  '#tlsplash .tlt{margin-top:14px;font-weight:600;font-size:clamp(12px,3.6vw,15px);letter-spacing:.3em;text-transform:uppercase;white-space:nowrap;color:#cfe9ff;opacity:0;'+
  'animation:tlTag 1.1s ease 1.4s forwards}'+
  '#tlsplash .tls{margin-top:12px;max-width:300px;font-size:13.5px;color:#8fa3c7;opacity:0;animation:tlFade .8s ease 2s forwards}'+
  '@keyframes tlEye{0%{opacity:0;transform:scale(.82)}60%{opacity:1}100%{opacity:1;transform:scale(1)}}'+
  '@keyframes tlGlow{0%,100%{filter:drop-shadow(0 0 10px rgba(26,168,255,.35))}50%{filter:drop-shadow(0 0 28px rgba(79,216,255,.75))}}'+
  '@keyframes tlUp{to{opacity:1;transform:none}}'+
  '@keyframes tlTag{0%{opacity:0;letter-spacing:.5em;text-shadow:0 0 0 rgba(79,216,255,0)}60%{opacity:1;text-shadow:0 0 18px rgba(79,216,255,.95)}100%{opacity:1;letter-spacing:.3em;text-shadow:0 0 10px rgba(79,216,255,.55)}}'+
  '@keyframes tlFade{to{opacity:1}}'+
  '#tlsplash.still .tle,#tlsplash.still .tln,#tlsplash.still .tlt,#tlsplash.still .tls{animation:tlFade .35s ease forwards;transform:none;letter-spacing:.3em;filter:none}'+
  '#tlsplash.still .tln{letter-spacing:-.01em}#tlsplash.still .tls{letter-spacing:normal}';
  var st=document.createElement('style');st.textContent=css;document.documentElement.appendChild(st);
  var d=document.createElement('div');d.id='tlsplash';d.setAttribute('role','presentation');if(reduce)d.className='still';
  d.innerHTML='<img class="tle" src="/splash-eye.png" alt="" width="380" height="225" decoding="async">'+
    '<div class="tln">Trade<i>Lens</i></div>'+
    '<div class="tlt">See the market clearly</div>'+
    '<div class="tls">AI-powered analysis for Deriv Synthetic Indices</div>';
  document.documentElement.appendChild(d);
  var prev=document.documentElement.style.overflow;document.documentElement.style.overflow='hidden';
  var done=false;
  function close(){
    if(done)return;done=true;d.classList.add('out');document.documentElement.style.overflow=prev;
    setTimeout(function(){if(d.parentNode)d.parentNode.removeChild(d);if(st.parentNode)st.parentNode.removeChild(st)},650);
  }
  d.addEventListener('click',close);
  setTimeout(close,reduce?900:3300);
})();
