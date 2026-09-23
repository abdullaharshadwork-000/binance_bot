DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agentic Binance Bot</title>
  <style>
    :root {
      --bg:#07111f; --panel:#0d1b2e; --panel2:#10223a; --line:#203754;
      --text:#e7eef8; --muted:#91a4bd; --blue:#3b82f6; --green:#22c55e;
      --red:#ef4444; --amber:#f59e0b; --cyan:#22d3ee;
    }
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}
    .wrap{max-width:1320px;margin:auto;padding:24px}
    .header{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:20px;flex-wrap:wrap}
    h1{font-size:30px;margin:0 0 5px}.sub{color:var(--muted);font-size:14px}
    .mode-pill,.badge{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:7px 11px;font-weight:700;font-size:12px;border:1px solid var(--line)}
    .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
    .paper{color:var(--cyan);background:#082a35}.testnet{color:var(--amber);background:#34270a}.live{color:var(--red);background:#351214}
    .controls{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 18px}
    button{border:0;border-radius:10px;padding:11px 16px;font-weight:800;color:white;cursor:pointer;transition:.15s}
    button:hover{transform:translateY(-1px);filter:brightness(1.06)}button:disabled{opacity:.55;cursor:wait;transform:none}
    .b-blue{background:var(--blue)}.b-green{background:#159447}.b-red{background:#c93434}.b-gray{background:#43546a}
    .notice{border:1px solid #7a5a14;background:#2b220d;color:#ffd878;border-radius:12px;padding:12px 14px;margin-bottom:18px;font-size:13px}
    .grid{display:grid;gap:14px}.stats{grid-template-columns:repeat(7,minmax(130px,1fr));margin-bottom:14px}
    .two{grid-template-columns:1.25fr .75fr;margin-bottom:14px}.three{grid-template-columns:repeat(3,1fr);margin-bottom:14px}
    .card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:15px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,.16)}
    .metric-label{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px}.metric-value{font-size:24px;font-weight:800}.metric-note{font-size:12px;color:var(--muted);margin-top:6px}
    h2{font-size:17px;margin:0 0 13px}.row{display:flex;justify-content:space-between;gap:20px;padding:8px 0;border-bottom:1px solid rgba(70,95,126,.28);font-size:13px}.row:last-child{border-bottom:0}.key{color:var(--muted)}.val{text-align:right;font-weight:700}
    .green{color:#66e18e}.red{color:#ff7979}.amber{color:#ffcc66}.blue{color:#7bb1ff}.muted{color:var(--muted)}
    .signal{font-size:25px;font-weight:900}.badge-green{color:#66e18e;background:#0c3020}.badge-red{color:#ff7979;background:#361719}.badge-amber{color:#ffcc66;background:#34270a}.badge-blue{color:#79c8ff;background:#0b2a3d}
    .progress{height:8px;background:#06101d;border-radius:999px;overflow:hidden;margin-top:8px}.progress>div{height:100%;background:var(--blue);border-radius:999px}
    .explain{margin-top:10px;padding:10px 12px;border-radius:10px;background:#091725;border:1px solid #1c334c;color:#b8c7da;font-size:12px;line-height:1.45}
    .chart-wrap{height:210px;position:relative}.chart-empty{height:100%;display:grid;place-items:center;color:var(--muted);font-size:13px}canvas{width:100%;height:100%}
    .market-card{margin-bottom:14px}.market-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap;margin-bottom:10px}.market-title-note{color:var(--muted);font-size:12px;margin-top:4px}.chart-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.chart-controls label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}.chart-controls select{background:#071522;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-weight:700;outline:none}.chart-controls select:focus{border-color:var(--blue)}
    .candle-wrap{height:430px;position:relative;border:1px solid #1b314a;border-radius:12px;background:#071421;overflow:hidden}.candle-wrap canvas{display:block;width:100%;height:100%}.candle-tooltip{position:absolute;display:none;pointer-events:none;z-index:3;top:10px;left:10px;padding:9px 11px;border-radius:9px;background:rgba(5,13,24,.94);border:1px solid #29425f;font-size:11px;line-height:1.55;color:#dbe7f6;min-width:190px}.candle-tooltip b{color:white}.chart-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:9px;color:var(--muted);font-size:11px}.legend-item{display:inline-flex;align-items:center;gap:6px}.legend-line{width:18px;height:3px;border-radius:2px;background:#7bb1ff}.legend-line.slow{background:#f59e0b}.legend-line.entry{background:#7bb1ff}.legend-line.stop{background:#ef4444}.legend-line.take{background:#22c55e}.legend-candle{width:9px;height:9px;border-radius:2px;background:#22c55e}.chart-status{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:7px}.chart-status span{font-size:11px;color:var(--muted)}
    .table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:10px 9px;border-bottom:1px solid rgba(70,95,126,.25);white-space:nowrap}th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase}tr:hover td{background:rgba(255,255,255,.018)}
    .footer{color:var(--muted);font-size:12px;text-align:right;padding:6px 2px 0}.loading{opacity:.72}
    .dashboard-stale .stats,.dashboard-stale .market-card,.dashboard-stale .two,.dashboard-stale .three{opacity:.62}
    .forming-note{color:var(--amber);font-weight:700}
    .help-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.help{padding:10px;border-radius:10px;background:#091725;border:1px solid #1c334c}.help b{display:block;margin-bottom:5px;font-size:12px}.help span{font-size:12px;color:var(--muted);line-height:1.45}
    details{margin-top:10px}summary{cursor:pointer;color:#7bb1ff;font-size:12px;font-weight:700}pre{white-space:pre-wrap;word-break:break-word;background:#06101d;border:1px solid var(--line);padding:12px;border-radius:10px;color:#b9c9db;max-height:330px;overflow:auto;font-size:11px}
    @media(max-width:1080px){.stats{grid-template-columns:repeat(3,1fr)}.two,.three{grid-template-columns:1fr}}
    @media(max-width:650px){.wrap{padding:15px}.stats{grid-template-columns:repeat(2,1fr)}.help-grid{grid-template-columns:1fr}.metric-value{font-size:20px}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div><h1>Agentic Binance Bot</h1><div class="sub">Trading decisions, risk, performance and bot health in one place.</div></div>
    <div id="modePill" class="mode-pill paper"><span class="dot"></span><span>PAPER</span></div>
  </div>

  <div id="liveNotice" class="notice" style="display:none"></div>

  <div class="controls">
    <button id="analyzeBtn" class="b-blue action" onclick="act('/bot/analyze','Running read-only market analysis…')">▶ Analyze Market</button>
    <button id="startBtn" class="b-green action" onclick="act('/bot/start','Starting automated checks…')">● Start Bot</button>
    <button id="stopBtn" class="b-red action" onclick="act('/bot/stop','Stopping bot…')">■ Stop Bot</button>
    <button class="b-gray action" onclick="loadAll(true)">↻ Refresh</button>
  </div>

  <div class="grid stats">
    <div class="card"><div class="metric-label">Bot status</div><div class="metric-value" id="running">—</div><div class="metric-note" id="cycleNote">—</div></div>
    <div class="card"><div class="metric-label" id="priceLabel">Asset price</div><div class="metric-value" id="price">—</div><div class="metric-note" id="symbol">—</div></div>
    <div class="card"><div class="metric-label">Account equity</div><div class="metric-value" id="equity">—</div><div class="metric-note" id="equityNote">Estimated current value</div></div>
    <div class="card"><div class="metric-label">Today's realized P/L</div><div class="metric-value" id="dailyPnl">—</div><div class="metric-note" id="dailyLimit">—</div></div>
    <div class="card"><div class="metric-label">Win rate</div><div class="metric-value" id="winRate">—</div><div class="metric-note" id="tradeCount">No closed trades</div></div>
    <div class="card"><div class="metric-label">Total realized P/L</div><div class="metric-value" id="totalPnl">—</div><div class="metric-note" id="profitFactor">Profit factor —</div></div>
    <div class="card"><div class="metric-label">Market feed</div><div class="metric-value" id="feedStatus">—</div><div class="metric-note" id="feedAge">Waiting for price stream</div></div>
  </div>

  <div class="card market-card">
    <div class="market-head">
      <div>
        <h2 style="margin-bottom:0">Live Candlestick Market Chart</h2>
        <div class="market-title-note">OHLC candles and volume from the same Binance market source used by this mode. Faded candles are still forming and are not used for strategy decisions.</div>
        <div class="chart-status"><span id="candleSource">Loading candles…</span><span>•</span><span id="candleUpdated">—</span><span>•</span><span id="strategyCandleStatus">Strategy candle —</span></div>
      </div>
      <div class="chart-controls">
        <label for="candleInterval">Chart interval</label>
        <select id="candleInterval" aria-label="Candlestick chart interval">
          <option value="1m">1m</option><option value="3m">3m</option><option value="5m">5m</option><option value="15m" selected>15m</option><option value="30m">30m</option><option value="1h">1h</option><option value="2h">2h</option><option value="4h">4h</option><option value="6h">6h</option><option value="8h">8h</option><option value="12h">12h</option><option value="1d">1d</option><option value="3d">3d</option><option value="1w">1w</option>
        </select>
        <label for="candleCount">Candles</label>
        <select id="candleCount" aria-label="Number of candles">
          <option value="60">60</option><option value="120" selected>120</option><option value="200">200</option>
        </select>
        <button class="b-gray" type="button" id="chartRefresh">↻ Chart</button>
      </div>
    </div>
    <div class="candle-wrap" id="candleWrap">
      <canvas id="candleChart" aria-label="Candlestick market chart"></canvas>
      <div class="candle-tooltip" id="candleTooltip"></div>
    </div>
    <div class="chart-legend">
      <span class="legend-item"><span class="legend-candle"></span> Green/red = candle direction</span>
      <span class="legend-item"><span class="legend-line"></span> EMA 20</span>
      <span class="legend-item"><span class="legend-line slow"></span> EMA 50</span>
      <span class="legend-item"><span class="legend-line entry"></span> Entry</span>
      <span class="legend-item"><span class="legend-line stop"></span> Stop</span>
      <span class="legend-item"><span class="legend-line take"></span> Take profit</span>
      <span class="legend-item"><span class="forming-note">◌</span> Faded = forming candle</span>
    </div>
  </div>

  <div class="grid two">
    <div class="card">
      <h2>Current Decision</h2>
      <div id="decision"><span class="muted">Click Run Once or Start Bot to generate a decision.</span></div>
    </div>
    <div class="card">
      <h2>Open Position</h2>
      <div id="position"><span class="muted">No open position.</span></div>
    </div>
  </div>

  <div class="grid three">
    <div class="card"><h2>Risk Engine</h2><div id="risk">No decision yet.</div></div>
    <div class="card"><h2>Last Order Attempt</h2><div id="execution">No BUY/SELL attempt yet.</div></div>
    <div class="card"><h2>Adaptive Learning</h2><div id="learning">Loading…</div></div>
  </div>

  <div class="grid two">
    <div class="card"><h2>Realized P/L Curve</h2><div class="chart-wrap" id="chartBox"><canvas id="pnlChart"></canvas></div><div class="explain">This chart uses closed trades only. A rising line means cumulative realized profit is increasing; a falling line means realized losses.</div></div>
    <div class="card"><h2>Performance Summary</h2><div id="performance">Loading…</div></div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>Recent Trades</h2>
    <div class="table-wrap" id="trades"><span class="muted">No trades yet.</span></div>
  </div>

  <div class="grid two">
    <div class="card">
      <h2>Safety & Configuration</h2>
      <div id="config">Loading…</div>
    </div>
    <div class="card">
      <h2>What the Dashboard Means</h2>
      <div class="help-grid">
        <div class="help"><b>BUY / HOLD / SELL</b><span>The strategy's current market opinion. A BUY still must pass the risk engine before any order is allowed.</span></div>
        <div class="help"><b>Confidence</b><span>How strongly the strategy supports its signal. It must reach the current threshold before an entry can be considered.</span></div>
        <div class="help"><b>Risk engine</b><span>The final deterministic safety gate. It controls position size, stop loss, exposure and the daily loss limit.</span></div>
        <div class="help"><b>Equity</b><span>Your estimated account value in the configured quote asset. In paper mode it combines simulated quote balance with the current value of the configured base-asset position.</span></div>
        <div class="help"><b>Win rate</b><span>Percentage of closed trades that made money. It should never be judged without average win/loss and total P/L.</span></div>
        <div class="help"><b>Profit factor</b><span>Gross winning P/L divided by gross losing P/L. It is shown only after enough closed trades exist to make it meaningful.</span></div>
        <div class="help"><b>1-second market monitor</b><span>While the engine is running, the latest market price is checked every second for stop loss and take profit conditions.</span></div>
        <div class="help"><b>Completed-candle signals</b><span>The strategy does not invent a new BUY or SELL every second. It recalculates on completed candles, which avoids reacting repeatedly to the same unfinished candle.</span></div>
      </div>
      <details><summary>Developer details / raw last cycle</summary><pre id="raw">No cycle yet.</pre></details>
    </div>
  </div>
  <div class="footer" id="updated">Not updated yet</div>
</div>
<script>
const $ = id => document.getElementById(id);
let baseAsset='BASE', quoteAsset='QUOTE';
const money = v => (v===null||v===undefined||Number.isNaN(Number(v)))?'—':quoteAsset+' '+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const num = (v,d=4) => (v===null||v===undefined||Number.isNaN(Number(v)))?'—':Number(v).toFixed(d);
const pct = (v,d=2) => (v===null||v===undefined||Number.isNaN(Number(v)))?'—':(Number(v)*100).toFixed(d)+'%';
const cls = v => Number(v)>0?'green':Number(v)<0?'red':'amber';
const esc = v => String(v??'—').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const badge = (text,type='blue') => `<span class="badge badge-${type}">${esc(text)}</span>`;
function sideBadge(side){ side=String(side||'HOLD').toUpperCase(); return badge(side,side==='BUY'?'green':side==='SELL'?'red':'amber'); }
function row(k,v){return `<div class="row"><span class="key">${k}</span><span class="val">${v}</span></div>`}
function syncActionButtons(){
  if(!latestDashboard)return;
  const running=Boolean(latestDashboard.running);
  if($('analyzeBtn')){$('analyzeBtn').disabled=running;$('analyzeBtn').title=running?'Stop the automated bot before running read-only analysis':'Read-only analysis; this button never submits an order'}
  if($('startBtn'))$('startBtn').disabled=running;
  if($('stopBtn'))$('stopBtn').disabled=!running;
}
function setLoading(on){
  document.querySelectorAll('.action').forEach(b=>b.disabled=on);
  if(!on)syncActionButtons();
  document.body.classList.toggle('loading',on)
}
function markDashboardStale(message){
  document.body.classList.add('dashboard-stale');
  $('liveNotice').style.display='block';
  $('liveNotice').textContent='Dashboard connection warning: '+message;
  $('running').innerHTML='<span class="amber">UNKNOWN</span>';
  $('feedStatus').innerHTML='<span class="red">STALE</span>';
  $('feedAge').textContent='Live dashboard updates are unavailable';
}

let candleData=[];
let candleMeta={};
let latestDashboard=null;
let candleHoverIndex=null;
let candleLoading=false;
let dashboardLoading=false;
let dashboardRequestSeq=0;
let candleRequestSeq=0;
let candleController=null;
let dashboardController=null;
let lastDashboardSuccessAt=0;

function chartPrice(v){
  if(v===null||v===undefined||Number.isNaN(Number(v)))return '—';
  const n=Number(v); return quoteAsset+' '+n.toLocaleString('en-US',{minimumFractionDigits:n>=1000?2:4,maximumFractionDigits:n>=1000?2:6});
}
function candleTime(ms){return new Date(Number(ms)).toLocaleString([], {month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'});}

async function loadCandles(force=false){
  if(candleLoading&&!force)return;
  if(force&&candleController)candleController.abort();
  const requestSeq=++candleRequestSeq;
  const controller=new AbortController();
  candleController=controller;
  candleLoading=true;
  try{
    const interval=$('candleInterval')?.value || latestDashboard?.config?.interval || '15m';
    const limit=Number($('candleCount')?.value||120);
    const r=await fetch(`/market/candles?interval=${encodeURIComponent(interval)}&limit=${limit}`,{signal:controller.signal});
    const j=await r.json(); if(!r.ok)throw new Error(j.detail||'Could not load candles');
    if(requestSeq!==candleRequestSeq)return;
    candleData=j.candles||[]; candleMeta=j;
    $('candleSource').textContent=`${j.market_source} · ${j.symbol} · ${j.interval}`;
    $('candleUpdated').textContent='Candles updated '+new Date().toLocaleTimeString();
    drawCandles();
  }catch(e){
    if(e.name!=='AbortError'&&requestSeq===candleRequestSeq)$('candleSource').textContent='Chart error: '+e.message;
  }finally{
    if(requestSeq===candleRequestSeq){
      candleLoading=false;
      if(candleController===controller)candleController=null;
    }
  }
}

function drawPriceLine(ctx,y,x0,x1,color,label,value,dash=[5,4]){
  if(!Number.isFinite(y))return; ctx.save();ctx.strokeStyle=color;ctx.lineWidth=1;ctx.setLineDash(dash);ctx.beginPath();ctx.moveTo(x0,y);ctx.lineTo(x1,y);ctx.stroke();ctx.setLineDash([]);
  ctx.font='10px Segoe UI';const text=`${label} ${chartPrice(value)}`;const tw=ctx.measureText(text).width+10;ctx.fillStyle='#07111f';ctx.fillRect(Math.max(x0,x1-tw),Math.max(1,y-9),tw,17);ctx.fillStyle=color;ctx.fillText(text,Math.max(x0+3,x1-tw+5),y+4);ctx.restore();
}

function drawCandles(){
  const canvas=$('candleChart'), wrap=$('candleWrap'); if(!canvas||!wrap||!candleData.length)return;
  const rect=wrap.getBoundingClientRect(), dpr=window.devicePixelRatio||1; if(rect.width<50||rect.height<100)return;
  canvas.width=Math.floor(rect.width*dpr);canvas.height=Math.floor(rect.height*dpr);canvas.style.width=rect.width+'px';canvas.style.height=rect.height+'px';
  const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);const w=rect.width,h=rect.height;ctx.clearRect(0,0,w,h);
  const left=12,right=82,top=16,bottom=24,volumeH=72,gap=12; const priceBottom=h-bottom-volumeH-gap;
  const e20=candleData.map(c=>Number.isFinite(Number(c.ema_fast))?Number(c.ema_fast):null);
  const e50=candleData.map(c=>Number.isFinite(Number(c.ema_slow))?Number(c.ema_slow):null);
  const overlay=[];const pos=latestDashboard?.open_trade;if(pos){overlay.push(Number(pos.entry_price),Number(pos.stop_price),Number(pos.take_profit_price))} if(latestDashboard?.price)overlay.push(Number(latestDashboard.price));
  let lo=Math.min(...candleData.map(c=>Number(c.low)),...overlay.filter(Number.isFinite)),hi=Math.max(...candleData.map(c=>Number(c.high)),...overlay.filter(Number.isFinite)); const span=Math.max(1e-9,hi-lo);lo-=span*.04;hi+=span*.04;
  const plotW=w-left-right, n=candleData.length, step=plotW/Math.max(1,n), bodyW=Math.max(1,Math.min(10,step*.62)); const X=i=>left+step*(i+.5),Y=v=>top+(hi-v)/(hi-lo)*(priceBottom-top);
  // background grid and price scale
  ctx.font='10px Segoe UI';ctx.textBaseline='middle';for(let i=0;i<=5;i++){const py=top+(priceBottom-top)*i/5;const pv=hi-(hi-lo)*i/5;ctx.strokeStyle='rgba(64,89,120,.28)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right,py);ctx.stroke();ctx.fillStyle='#91a4bd';ctx.fillText(chartPrice(pv),w-right+7,py)}
  // volume
  const vmax=Math.max(1,...candleData.map(c=>Number(c.volume)||0));candleData.forEach((c,i)=>{const x=X(i),vol=(Number(c.volume)||0)/vmax*volumeH;const up=Number(c.close)>=Number(c.open);ctx.fillStyle=up?'rgba(34,197,94,.28)':'rgba(239,68,68,.28)';ctx.fillRect(x-bodyW/2,h-bottom-vol,bodyW,vol)});
  ctx.fillStyle='#71849c';ctx.fillText('VOL',left,h-bottom-volumeH-5);
  // candles
  candleData.forEach((c,i)=>{const x=X(i),o=Y(Number(c.open)),cl=Y(Number(c.close)),high=Y(Number(c.high)),low=Y(Number(c.low));const up=Number(c.close)>=Number(c.open),color=up?'#22c55e':'#ef4444';ctx.save();if(c.is_closed===false)ctx.globalAlpha=.42;ctx.strokeStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,high);ctx.lineTo(x,low);ctx.stroke();ctx.fillStyle=color;const y=Math.min(o,cl),bh=Math.max(1.4,Math.abs(cl-o));ctx.fillRect(x-bodyW/2,y,bodyW,bh);ctx.restore()});
  // EMAs
  function line(series,color){ctx.strokeStyle=color;ctx.lineWidth=1.35;ctx.beginPath();let started=false;series.forEach((v,i)=>{if(v===null)return;const x=X(i),y=Y(v);if(!started){ctx.moveTo(x,y);started=true}else ctx.lineTo(x,y)});ctx.stroke()}
  line(e20,'#7bb1ff');line(e50,'#f59e0b');
  // current price and open trade levels
  const current=Number(latestDashboard?.price);if(Number.isFinite(current))drawPriceLine(ctx,Y(current),left,w-right,'#22d3ee','NOW',current,[2,3]);
  if(pos){drawPriceLine(ctx,Y(Number(pos.entry_price)),left,w-right,'#7bb1ff','ENTRY',Number(pos.entry_price));drawPriceLine(ctx,Y(Number(pos.stop_price)),left,w-right,'#ef4444','STOP',Number(pos.stop_price));drawPriceLine(ctx,Y(Number(pos.take_profit_price)),left,w-right,'#22c55e','TAKE',Number(pos.take_profit_price));}
  const signalClose=Number(latestDashboard?.last_cycle?.signal_candle_close_time);
  const signalIndex=Number.isFinite(signalClose)?candleData.findIndex(c=>Number(c.close_time)===signalClose):-1;
  if(signalIndex>=0){const sx=X(signalIndex);ctx.save();ctx.strokeStyle='#22d3ee';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(sx,top);ctx.lineTo(sx,priceBottom);ctx.stroke();ctx.restore()}
  // time labels
  ctx.fillStyle='#71849c';ctx.textBaseline='alphabetic';const marks=5;for(let j=0;j<=marks;j++){const i=Math.min(n-1,Math.floor((n-1)*j/marks));const txt=new Date(Number(candleData[i].open_time)).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});ctx.fillText(txt,Math.max(left,X(i)-18),h-7)}
  // hover crosshair
  if(candleHoverIndex!==null&&candleHoverIndex>=0&&candleHoverIndex<n){const i=candleHoverIndex,c=candleData[i],x=X(i);ctx.strokeStyle='rgba(210,225,244,.35)';ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,h-bottom);ctx.stroke();ctx.setLineDash([]);}
}

function showCandleTooltip(ev){
  if(!candleData.length)return;const wrap=$('candleWrap'),rect=wrap.getBoundingClientRect(),left=12,right=82,plotW=rect.width-left-right;const x=ev.clientX-rect.left;if(x<left||x>rect.width-right){candleHoverIndex=null;$('candleTooltip').style.display='none';drawCandles();return}
  const i=Math.max(0,Math.min(candleData.length-1,Math.floor((x-left)/(plotW/candleData.length))));candleHoverIndex=i;const c=candleData[i],tip=$('candleTooltip');const up=Number(c.close)>=Number(c.open);tip.innerHTML=`<b>${candleTime(c.open_time)}</b> · ${c.is_closed===false?'<span class="amber">FORMING</span>':'CLOSED'}<br>O ${chartPrice(c.open)} &nbsp; H ${chartPrice(c.high)}<br>L ${chartPrice(c.low)} &nbsp; C <span class="${up?'green':'red'}">${chartPrice(c.close)}</span><br>Volume ${Number(c.volume).toLocaleString('en-US',{maximumFractionDigits:4})}`;tip.style.display='block';tip.style.left=Math.min(rect.width-205,Math.max(8,x+14))+'px';tip.style.top='10px';drawCandles();
}

async function act(url,msg){
  if(url==='/bot/start'&&latestDashboard?.config?.mode==='live'&&latestDashboard?.config?.live_orders_allowed){
    const ok=confirm('LIVE ORDERS ARE ENABLED. Starting the bot can use real funds. Continue?');
    if(!ok)return;
  }
  setLoading(true); $('updated').textContent=msg;
  try{const r=await fetch(url,{method:'POST'});const j=await r.json();if(!r.ok)throw new Error(j.detail||'Request failed');await loadAll(false,true)}
  catch(e){alert(e.message);$('updated').textContent='Action failed: '+e.message}
  finally{setLoading(false)}
}

function renderDecision(cycle){
  if(!cycle?.signal){$('decision').innerHTML='<span class="muted">No market decision yet.</span>';return}
  const s=cycle.signal, f=s.features||{}, threshold=cycle.learning?.confidence_threshold;
  const c=Math.max(0,Math.min(1,Number(s.confidence||0)));
  $('decision').innerHTML=`
    <div style="display:flex;justify-content:space-between;gap:15px;align-items:center;flex-wrap:wrap">
      <div><div class="metric-label">Signal</div><div class="signal">${sideBadge(s.side)}</div></div>
      <div style="min-width:230px"><div class="row"><span class="key">Confidence</span><span class="val">${pct(c)}</span></div><div class="progress"><div style="width:${c*100}%"></div></div><div class="metric-note">Required threshold: ${pct(threshold)}</div></div>
    </div>
    <div class="explain"><b>Why:</b> ${esc(s.reason)}</div>
    <div style="margin-top:10px">${row('RSI',num(f.rsi,2))}${row('Fast EMA',num(f.ema_fast,2))}${row('Slow EMA',num(f.ema_slow,2))}${row('5-candle momentum',pct(f.momentum_5))}${row('Volume ratio',num(f.volume_ratio,2)+'×')}${row('ATR / volatility',pct(f.atr_pct))}</div>`;
}

function renderRisk(cycle){
  const r=cycle?.risk;
  if(!r){$('risk').innerHTML=cycle?.open_trade?'<span class="muted">Entry risk check is skipped while a position is already open.</span>':'<span class="muted">No entry risk check yet.</span>';return}
  $('risk').innerHTML=`${row('Entry',r.allowed?badge('ALLOWED','green'):badge('BLOCKED','red'))}${row('Reason',esc(r.reason))}${row('Quantity',num(r.quantity,8))}${row('Stop loss',money(r.stop_price))}${row('Take profit',money(r.take_profit_price))}<div class="explain">A signal cannot open a new position unless this panel says ALLOWED.</div>`;
}

function renderExecution(e){
  if(!e){$('execution').innerHTML='<span class="muted">No BUY/SELL attempt has been recorded in this process.</span>';return}
  const action=String(e.action||'NONE').toUpperCase();
  $('execution').innerHTML=`${row('Action',sideBadge(action))}${row('Result',e.success?badge('SUCCESS','green'):badge('FAILED','red'))}${row('Time',e.timestamp?new Date(e.timestamp).toLocaleString():'—')}${row('Message',esc(e.message))}${e.details?row('Details',esc(JSON.stringify(e.details))):''}`;
}

function renderPosition(d){
  const t=d.open_trade;if(!t){$('position').innerHTML='<span class="muted">No open position right now.</span>';return}
  const upnl=d.open_trade_metrics?.unrealized_pnl, upct=d.open_trade_metrics?.unrealized_pnl_pct;
  $('position').innerHTML=`${row('Status',badge('OPEN','blue'))}${row('Quantity',num(t.quantity,8))}${row('Entry price',money(t.entry_price))}${row('Current price',money(d.price))}${row('Unrealized P/L',`<span class="${cls(upnl)}">${money(upnl)} (${pct(upct)})</span>`)}${row('Stop loss',money(t.stop_price))}${row('Take profit',money(t.take_profit_price))}${row('Entry reason',esc(t.entry_reason))}`;
}

function renderLearning(l){
  if(!l){$('learning').innerHTML='No learning profile.';return}
  $('learning').innerHTML=`${row('Status',window.cfg?.adaptive_learning?badge('ENABLED','green'):badge('DISABLED','amber'))}${row('Confidence threshold',pct(l.confidence_threshold))}${row('Risk multiplier',num(l.risk_multiplier,2)+'×')}${row('Learning samples',l.samples??0)}${row('Recent learned win rate',pct(l.last_win_rate))}${row('Recent avg P/L',pct(l.last_avg_pnl_pct))}<div class="explain">Learning may adjust confidence and risk conservatively. It does not remove the deterministic risk limits.</div>`;
}

function renderPerformance(p){
  const pf=p.profit_factor===null?'—':(!Number.isFinite(Number(p.profit_factor))?'∞':num(p.profit_factor,2));
  $('performance').innerHTML=`${row('Closed trades',p.closed_trades)}${row('Wins / losses',`${p.wins} / ${p.losses}`)}${row('Average trade P/L',money(p.avg_pnl))}${row('Average trade return',pct(p.avg_pnl_pct))}${row('Best trade',`<span class="green">${money(p.best_trade)}</span>`)}${row('Worst trade',`<span class="red">${money(p.worst_trade)}</span>`)}${row('Profit factor',pf)}${row('Max realized drawdown',`<span class="red">${money(p.max_drawdown)}</span>`)}`;
}

function renderTrades(ts){
  if(!ts?.length){$('trades').innerHTML='<span class="muted">No trades have been recorded yet.</span>';return}
  const rows=ts.map(t=>`<tr><td>#${t.id}</td><td>${esc(t.symbol)}</td><td>${t.status==='OPEN'?badge('OPEN','blue'):badge('CLOSED','green')}</td><td>${num(t.quantity,8)}</td><td>${money(t.entry_price)}</td><td>${money(t.exit_price)}</td><td class="${cls(t.pnl)}">${money(t.pnl)}</td><td class="${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td><td>${esc(t.exit_reason||t.entry_reason)}</td></tr>`).join('');
  $('trades').innerHTML=`<table><thead><tr><th>ID</th><th>Pair</th><th>Status</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P/L</th><th>Return</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function drawChart(curve){
  const box=$('chartBox'); if(!curve?.length){box.innerHTML='<div class="chart-empty">The P/L curve will appear after the first closed trade.</div>';return}
  if(!$('pnlChart'))box.innerHTML='<canvas id="pnlChart"></canvas>';
  const c=$('pnlChart'), ctx=c.getContext('2d'), rect=c.getBoundingClientRect(), dpr=window.devicePixelRatio||1;c.width=rect.width*dpr;c.height=rect.height*dpr;ctx.scale(dpr,dpr);
  const w=rect.width,h=rect.height,pad=24, vals=curve.map(x=>Number(x.cumulative_pnl));let lo=Math.min(0,...vals),hi=Math.max(0,...vals);if(hi===lo){hi+=1;lo-=1}const X=i=>pad+(i/(Math.max(1,vals.length-1)))*(w-pad*2),Y=v=>h-pad-((v-lo)/(hi-lo))*(h-pad*2);
  ctx.clearRect(0,0,w,h);ctx.strokeStyle='#203754';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(pad,Y(0));ctx.lineTo(w-pad,Y(0));ctx.stroke();ctx.strokeStyle=vals.at(-1)>=0?'#22c55e':'#ef4444';ctx.lineWidth=2;ctx.beginPath();vals.forEach((v,i)=>i?ctx.lineTo(X(i),Y(v)):ctx.moveTo(X(i),Y(v)));ctx.stroke();ctx.fillStyle='#91a4bd';ctx.font='11px Segoe UI';ctx.fillText(money(hi),2,14);ctx.fillText(money(lo),2,h-5);
}

function renderConfig(d){
  const c=d.config;window.cfg=c;
  $('config').innerHTML=`${row('Mode',badge(c.mode.toUpperCase(),c.mode==='live'?'red':c.mode==='testnet'?'amber':'blue'))}${row('Pair / strategy candle',`${esc(c.symbol)} · ${esc(c.interval)}`)}${row('Market / risk monitor',`Every ${c.cycle_seconds}s`)}${row('Price stream',c.use_websocket_market_data?badge('WEBSOCKET','green'):badge('REST','amber'))}${row('Strategy timing',`New completed ${esc(c.interval)} candle`)}${row('Account refresh',`Every ${c.account_refresh_seconds}s in testnet/live`)}${row('Paper slippage model',`${num(c.paper_slippage_bps,1)} bps per fill`)}${row('Risk per trade',pct(c.risk_per_trade))}${row('Max position allocation',pct(c.max_position_fraction))}${row('Daily loss stop',pct(c.max_daily_loss_fraction))}${row('Stop loss',pct(c.stop_loss_pct))}${row('Take profit',pct(c.take_profit_pct))}${row('Base signal threshold',pct(c.min_signal_confidence))}${row('Adaptive learning',c.adaptive_learning?badge('ON','green'):badge('OFF','amber'))}${row('LLM advisor',c.llm_advisor?badge('ON','blue'):badge('OFF','amber'))}${row('Live orders',c.live_orders_allowed?badge('ENABLED','red'):badge('BLOCKED','green'))}`;
}

async function loadAll(showLoading=false,force=false){
  if(dashboardLoading&&!force)return;
  if(force&&dashboardController)dashboardController.abort();
  const requestSeq=++dashboardRequestSeq;
  const controller=new AbortController();
  dashboardController=controller;
  dashboardLoading=true;
  if(showLoading)setLoading(true);
  try{
    const r=await fetch('/dashboard-data',{signal:controller.signal});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Dashboard request failed');
    if(requestSeq!==dashboardRequestSeq)return;
    latestDashboard=d;
    lastDashboardSuccessAt=Date.now();
    document.body.classList.remove('dashboard-stale');
    baseAsset=d.config.base_asset||'BASE';
    quoteAsset=d.config.quote_asset||'QUOTE';
    $('priceLabel').textContent=baseAsset+' price';
    $('equityNote').textContent='Estimated current value in '+quoteAsset;
    drawCandles();
    const mode=d.config.mode;$('modePill').className='mode-pill '+mode;$('modePill').innerHTML=`<span class="dot"></span><span>${mode.toUpperCase()} MODE</span>`;
    if(d.engine_error){$('liveNotice').style.display='block';$('liveNotice').textContent='Engine warning: '+d.engine_error}
    else if(d.market_error){$('liveNotice').style.display='block';$('liveNotice').textContent='Market/account data warning: '+d.market_error}
    else if(mode==='live'){$('liveNotice').style.display='block';$('liveNotice').textContent=d.config.live_orders_allowed?'LIVE ORDERS ARE ENABLED. Real funds can be used.':'Live mode selected, but real orders are blocked because ALLOW_LIVE_TRADING=false.'}
    else $('liveNotice').style.display='none';
    $('running').innerHTML=d.running?'<span class="green">RUNNING</span>':'<span class="red">STOPPED</span>';
    syncActionButtons();
    $('cycleNote').textContent=`Risk check every ${d.config.cycle_seconds}s · strategy on closed ${d.config.interval} candles`;
    $('price').textContent=money(d.price);$('symbol').textContent=`${d.config.symbol} · ${d.market_monitor?.source||'waiting'}`;$('equity').textContent=money(d.equity);
    const mm=d.market_monitor||{};
    if(!d.running){$('feedStatus').innerHTML='<span class="amber">STOPPED</span>';$('feedAge').textContent='Start Bot to open the live price stream'}
    else if(mm.websocket_connected){$('feedStatus').innerHTML='<span class="green">LIVE</span>';$('feedAge').textContent=`WebSocket · price age ${Math.round(mm.price_age_ms||0)} ms`}
    else{$('feedStatus').innerHTML='<span class="amber">FALLBACK</span>';$('feedAge').textContent=`${mm.source||'REST'} · WebSocket not fresh`}
    $('dailyPnl').innerHTML=`<span class="${cls(d.daily_realized_pnl)}">${money(d.daily_realized_pnl)}</span>`;$('dailyLimit').textContent=`Daily stop: ${money(d.daily_loss_limit_amount)} loss`;
    $('winRate').textContent=pct(d.performance.win_rate);$('tradeCount').textContent=`${d.performance.closed_trades} closed trades`;$('totalPnl').innerHTML=`<span class="${cls(d.performance.total_pnl)}">${money(d.performance.total_pnl)}</span>`;
    const pf=d.performance.profit_factor;$('profitFactor').textContent='Profit factor '+(pf===null?'—':(!Number.isFinite(Number(pf))?'∞':num(pf,2)));
    const signalClose=Number(d.last_cycle?.signal_candle_close_time);
    $('strategyCandleStatus').textContent=Number.isFinite(signalClose)?'Strategy candle '+new Date(signalClose).toLocaleString():'Strategy candle —';
    renderConfig(d);renderDecision(d.last_cycle);renderRisk(d.last_cycle);renderExecution(d.last_execution);renderPosition(d);renderLearning(d.learning);renderPerformance(d.performance);renderTrades(d.trades);drawChart(d.performance.equity_curve);$('raw').textContent=JSON.stringify(d.last_cycle,null,2);
    $('updated').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){
    if(e.name!=='AbortError'&&requestSeq===dashboardRequestSeq){
      $('updated').textContent='Dashboard error: '+e.message;
      markDashboardStale(e.message);
    }
  }finally{
    if(requestSeq===dashboardRequestSeq){
      dashboardLoading=false;
      if(dashboardController===controller)dashboardController=null;
      if(showLoading)setLoading(false);
    }
  }
}
$('candleInterval').addEventListener('change',()=>loadCandles(true));
$('candleCount').addEventListener('change',()=>loadCandles(true));
$('chartRefresh').addEventListener('click',()=>loadCandles(true));
$('candleWrap').addEventListener('mousemove',showCandleTooltip);
$('candleWrap').addEventListener('mouseleave',()=>{candleHoverIndex=null;$('candleTooltip').style.display='none';drawCandles()});
loadAll(true,true).then(()=>{if(latestDashboard?.config?.interval&&$('candleInterval').querySelector(`option[value="${latestDashboard.config.interval}"]`))$('candleInterval').value=latestDashboard.config.interval;loadCandles(true)});
setInterval(()=>loadAll(false,false),1000);setInterval(()=>loadCandles(false),5000);setInterval(()=>{if(lastDashboardSuccessAt&&Date.now()-lastDashboardSuccessAt>5000)markDashboardStale('No successful dashboard update for more than 5 seconds')},1000);window.addEventListener('resize',()=>{drawChart(latestDashboard?.performance?.equity_curve||[]);drawCandles()});
</script>
</body></html>'''
