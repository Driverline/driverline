// Helpers for the "Enter this in MT5" ticket on the Trade Card.
(function(){
  const get=(k,d)=>{try{const v=localStorage.getItem(k);return v===null?d:JSON.parse(v)}catch(e){return d}};
  const dec=s=>{const t=String(s);return t.includes('.')?t.split('.')[1].length:0};
  const norm=s=>String(s).toLowerCase().replace(/index/g,'').replace(/[^a-z0-9]/g,'');
  // Lot size from the saved contract specs, balance and risk % (same maths as the Risk page).
  window.dlLots=function(key,entry,stop){
    const sp=get('dl_spec_'+key,null),bal=Number(get('dl_bal',0)),rp=Number(get('dl_rp',1));
    if(!sp)return null;
    const unit=sp.ts>0&&sp.tv>0?sp.tv/sp.ts:(sp.cs>0?sp.cs:0),dist=Math.abs(entry-stop);
    if(!(unit>0&&sp.vmin>0&&sp.vstep>0&&bal>0&&rp>0&&dist>0))return null;
    let lots=Math.floor(bal*rp/100/(dist*unit)/sp.vstep+1e-9)*sp.vstep,warn=false;
    if(lots<sp.vmin){lots=sp.vmin;warn=true}
    if(sp.vmax>0&&lots>sp.vmax)lots=sp.vmax;
    return{lots:Number(lots.toFixed(Math.max(dec(sp.vstep),2))),warn};
  };
  // The exact symbol name MT5 uses, when the companion EA has reported it.
  let specs=null;
  window.dlSymbolName=async function(name){
    try{if(!specs)specs=await (await fetch('/api/ea/specs')).json();
      const m=specs.find(x=>norm(x.symbol)===norm(name));return m?m.symbol:name}catch(e){return name}
  };
})();
