"""Local, read-only BTC/XAUT options dashboard for Delta Exchange India."""

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


HOST = "127.0.0.1"
PORT = 8765
API_URL = "https://api.india.delta.exchange/v2/tickers"
CACHE_TTL_SECONDS = 1.5
cache = {}
cache_lock = threading.Lock()

PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delta Options Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2c;--line:#26334c;--muted:#9eabc1;--text:#edf3ff;--green:#83e0b2;--red:#ff9eaa;--gold:#f5cf76}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}header{padding:22px 26px;border-bottom:1px solid var(--line);background:#0e1526}h1{margin:0 0 5px;font-size:22px}header p,.muted{color:var(--muted);margin:0}.wrap{padding:20px 26px}.controls{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:16px}.control label{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}select,button{background:var(--panel);color:var(--text);border:1px solid var(--line);padding:9px 12px;border-radius:8px;font-size:14px}button{cursor:pointer}.stats{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}.stat{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px 14px;min-width:140px}.stat small{display:block;color:var(--muted)}.stat strong{font-size:17px}.note{padding:11px 14px;background:#1a2132;border:1px solid var(--line);border-radius:8px;color:#c6d0df;margin:14px 0}.tablebox{overflow:auto;border:1px solid var(--line);border-radius:9px;max-height:calc(100vh - 315px)}table{border-collapse:collapse;width:100%;white-space:nowrap;font-variant-numeric:tabular-nums}thead{position:sticky;top:0;background:#18233a;z-index:1}th,td{text-align:right;padding:9px 11px;border-bottom:1px solid #202b40}th{font-size:12px;color:#c4cee0}th:first-child,td:first-child,th:last-child,td:last-child{text-align:left}.call{color:var(--green)}.put{color:var(--red)}tr.watch{background:#28301f}tr.watch td:first-child:after{content:'  WATCH';font-size:10px;color:var(--gold);font-weight:700}.status{color:var(--muted);margin:10px 0}.error{color:var(--red)}@media(max-width:650px){.wrap{padding:14px}.tablebox{max-height:65vh}}
</style></head><body>
<header><h1>Delta Exchange India · Options</h1><p>BTC and XAUT live option chains · read-only · refreshes every 2 seconds</p></header>
<main class="wrap"><div class="controls"><div class="control"><label for="asset">Underlying</label><select id="asset"><option>BTC</option><option>XAUT</option></select></div><div class="control"><label for="expiry">Expiry</label><select id="expiry"><option value="">Loading…</option></select></div><button id="refresh">Refresh now</button></div>
<div class="stats"><div class="stat"><small>Underlying spot</small><strong id="spot">—</strong></div><div class="stat"><small>Contracts shown</small><strong id="count">—</strong></div><div class="stat"><small>Nearest listed strike (ATM)</small><strong id="atm">—</strong></div></div>
<section class="note" style="display:grid;gap:10px"><div><b>ATM short straddle quote check</b> · hypothetical entry credit P = ATM call bid + ATM put bid; stop = 2P; target = P/2.</div><div id="straddle" class="muted">Waiting for chain data…</div><div id="paperstatus" class="muted">Live execution is not connected.</div><small class="muted">All displayed P/L and stop/target calculations use bid for the hypothetical sell credit and ask for the estimated buyback cost. Since account fills are not connected, these are quote-based levels, not live position P/L. Mark and last price are excluded.</small></section>
<div class="status" id="status">Connecting to Delta Exchange India…</div><div class="tablebox"><table><thead><tr><th>Type</th><th>Strike</th><th>Bid</th><th>Ask</th><th>Delta</th><th>Gamma</th><th>Theta</th><th>Vega</th><th>IV</th><th>Open interest</th><th>Volume</th><th>Symbol</th></tr></thead><tbody id="rows"></tbody></table></div></main>
<script>
const $=id=>document.getElementById(id);let latest=null;const fmt=(v,d=4)=>{let n=Number(v);return v===null||v===undefined||!Number.isFinite(n)?'—':n.toLocaleString(undefined,{maximumFractionDigits:d})};
function expiryFromSymbol(s){let m=(s||'').match(/-(\d{2})(\d{2})(\d{2})$/);if(!m)return '';return `${m[1]}-${m[2]}-20${m[3]}`}
function labelDate(s){let m=s.match(/^(\d{2})-(\d{2})-(\d{4})$/);return m?`${m[1]}-${m[2]}-${m[3]}`:s}
async function update(){try{let asset=$('asset').value;let response=await fetch('/api/chain?asset='+encodeURIComponent(asset),{cache:'no-store'});let data=await response.json();if(!response.ok)throw Error(data.error||'Request failed');latest=data;let expiries=[...new Set(data.rows.map(r=>expiryFromSymbol(r.symbol)).filter(Boolean))].sort((a,b)=>{let [da,ma,ya]=a.split('-'),[db,mb,yb]=b.split('-');return new Date(+ya,+ma-1,+da)-new Date(+yb,+mb-1,+db)});let sel=$('expiry'),previous=sel.value;sel.innerHTML='';for(let e of expiries){let o=document.createElement('option');o.value=e;o.textContent=labelDate(e);sel.append(o)}if(expiries.includes(previous))sel.value=previous;else if(expiries.length)sel.selectedIndex=0;render();$('status').textContent=`Updated ${new Date(data.fetched_at).toLocaleTimeString()} · next refresh in 2 sec`;$('status').className='status'}catch(e){$('status').textContent='Feed error: '+e.message;$('status').className='status error'}}
function render(){if(!latest)return;let exp=$('expiry').value;let rs=latest.rows.filter(r=>expiryFromSymbol(r.symbol)===exp);let spot=Number(rs.find(r=>r.spot_price)?.spot_price||latest.spot||0);let strikes=[...new Set(rs.map(r=>Number(r.strike_price)).filter(Number.isFinite))].sort((a,b)=>a-b);let ix=strikes.length?strikes.reduce((best,x,i)=>Math.abs(x-spot)<Math.abs(strikes[best]-spot)?i:best,0):-1;let atm=ix>=0?strikes[ix]:null;$('spot').textContent=fmt(spot,4);$('atm').textContent=atm===null?'—':fmt(atm,4);$('count').textContent=rs.length;let watch=new Map();if(atm!==null){let c=rs.find(r=>r.contract_type==='call_options'&&Number(r.strike_price)===atm);let p=rs.find(r=>r.contract_type==='put_options'&&Number(r.strike_price)===atm);if(c)watch.set(c.symbol,'ATM STRADDLE CALL');if(p)watch.set(p.symbol,'ATM STRADDLE PUT');let cbid=Number(c?.quotes?.best_bid),pbid=Number(p?.quotes?.best_bid),cask=Number(c?.quotes?.best_ask),pask=Number(p?.quotes?.best_ask);let entry=cbid+pbid,exit=cask+pask,stop=entry*2,target=entry/2,pnl=entry-exit;$('straddle').textContent=`ATM ${fmt(atm)} · hypothetical sell credit (call bid + put bid): ${fmt(entry)} · buyback ask (call ask + put ask): ${fmt(exit)} · hypothetical P/L (bid credit − ask cost): ${fmt(pnl)} · stop level: ${fmt(stop)} · target level: ${fmt(target)}`;$('paperstatus').textContent='Quote levels recalculate every refresh. They do not track any filled account position.'}
rs.sort((a,b)=>Number(a.strike_price)-Number(b.strike_price)||(a.contract_type||'').localeCompare(b.contract_type||''));let body=document.createDocumentFragment();for(let r of rs){let q=r.quotes||{},g=r.greeks||{},type=r.contract_type==='call_options'?'CALL':'PUT',tr=document.createElement('tr');if(watch.has(r.symbol))tr.className='watch';let vals=[type,fmt(r.strike_price),fmt(q.best_bid),fmt(q.best_ask),fmt(g.delta,6),fmt(g.gamma,8),fmt(g.theta,6),fmt(g.vega,6),fmt(q.mark_iv??r.mark_vol,6),fmt(r.oi),fmt(r.volume),r.symbol];for(let i=0;i<vals.length;i++){let td=document.createElement('td');td.textContent=vals[i];if(i===0)td.className=type==='CALL'?'call':'put';if(i===11&&watch.has(r.symbol))td.textContent+=' · '+watch.get(r.symbol);tr.append(td)}body.append(tr)}$('rows').replaceChildren(body)}
$('asset').addEventListener('change',()=>{latest=null;update()});$('expiry').addEventListener('change',render);$('refresh').addEventListener('click',update);update();setInterval(update,2000);
</script></body></html>'''


def fetch_chain(asset):
    now = time.monotonic()
    with cache_lock:
        cached = cache.get(asset)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
    query = urlencode({
        "contract_types": "call_options,put_options",
        "underlying_asset_symbols": asset,
    })
    request = Request(f"{API_URL}?{query}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not payload.get("success"):
        raise RuntimeError("Delta Exchange returned an unsuccessful response")
    rows = payload.get("result", [])
    value = {"asset": asset, "rows": rows, "fetched_at": datetime.now().astimezone().isoformat()}
    with cache_lock:
        cache[asset] = (time.monotonic(), value)
    return value


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, content_type, body):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.respond(200, "text/html; charset=utf-8", PAGE)
        elif parsed.path == "/api/chain":
            asset = parse_qs(parsed.query).get("asset", ["BTC"])[0].upper()
            if asset not in ("BTC", "XAUT"):
                self.respond(400, "application/json; charset=utf-8", json.dumps({"error": "Asset must be BTC or XAUT"}))
                return
            try:
                self.respond(200, "application/json; charset=utf-8", json.dumps(fetch_chain(asset), separators=(",", ":")))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
                self.respond(502, "application/json; charset=utf-8", json.dumps({"error": str(exc)}))
        else:
            self.respond(404, "text/plain; charset=utf-8", "Not found")

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Dashboard running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop. This dashboard is read-only and uses public market data.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
