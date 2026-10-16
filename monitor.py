"""Public OTC snapshot monitor. Standard library only."""
import argparse
import json
import math
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import closing
import adaptive
import otc_radar as radar
import research
import trend_history

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'monitor.sqlite3'
BASE = 'https://otcdesks.cash/api/coins'
SOCIAL_LOCK = threading.Lock()

def social_links(c, mint):
    if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', mint):
        raise ValueError('无效合约地址')
    if not c.execute('SELECT 1 FROM coins WHERE mint=?', (mint,)).fetchone():
        raise ValueError('代币尚未被本地观察')
    with SOCIAL_LOCK:
        key = 'social:' + mint
        row = c.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        cached = json.loads(row[0]) if row else None
        if cached and time.time()-cached['at'] < 3600:
            return cached
        coins = radar.Sources().coins(mint=mint)['coins']
        coin = next((x for x in coins if x.get('mint') == mint), None)
        if coin is None:
            raise ValueError('OTC 未返回该代币详情')
        result = dict(at=time.time(), twitter=coin.get('twitter') or '')
        state(c, key, result)
        c.commit()
        return result

def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS coins(mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, created REAL, seen REAL);
    CREATE TABLE IF NOT EXISTS samples(mint TEXT, ts REAL, price REAL, liquidity REAL, holders REAL, volume REAL, PRIMARY KEY(mint,ts));
    CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, mint TEXT, ts REAL, detail TEXT);
    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
    ''')
    trend_history.setup(c)
    return c

def state(c, key, value):
    c.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))

def number(x):
    try:
        n = float(x)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError):
        return None

def signal(rows, now):
    if not rows or now - rows[-1]['ts'] > 180 or rows[-1]['ts'] > now + 60:
        return None
    last = rows[-1]
    candidates = [r for r in rows if 240 <= last['ts'] - r['ts'] <= 600]
    if not candidates:
        return None
    first = min(candidates, key=lambda r: abs(last['ts'] - r['ts'] - 300))
    window = [r for r in rows if r['ts'] >= first['ts']]
    if len(window) < 3 or any(b['ts']-a['ts'] > 240 for a,b in zip(window,window[1:])):
        return None
    if any(r[k] is None for r in (first,last) for k in ('price','holders','liquidity')) or first['price'] <= 0 or first['liquidity'] <= 0:
        return None
    gain = (last['price']/first['price']-1)*100
    holders = last['holders']-first['holders']
    if not (3 <= gain <= 30 and holders >= 3 and last['holders'] >= 20 and last['liquidity'] >= 5000 and last['liquidity'] >= first['liquidity']*.95):
        return None
    if last['price'] < max(r['price'] or 0 for r in window)*.95:
        return None
    return {'gain':round(gain,2),'holdersAdded':int(holders),'minutes':round((last['ts']-first['ts'])/60,1),'liquidity':round(last['liquidity'],2)}

def fetch(page, query=None, sort='new'):
    params = {'page':page,'size':60,'sort':sort,'dir':'desc'}
    if query:
        params['q'] = query
    url = BASE + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent':'OTCTrendMonitor/1.0','Accept':'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    if not isinstance(data.get('coins'),list) or not isinstance(data.get('total'),(int,float)):
        raise ValueError('Unexpected API schema')
    return data

def ingest(c, data):
    now = time.time()
    for coin in data['coins']:
        mint = coin.get('mint')
        if not isinstance(mint,str) or not mint:
            continue
        c.execute('INSERT OR REPLACE INTO coins VALUES (?,?,?,?,?)',(mint,coin.get('name',''),coin.get('symbol',''),number(coin.get('createdAt')),now))
        s = coin.get('snapshot') or {}
        ts = number(s.get('at'))
        if not ts or ts > now+60:
            continue
        c.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?)',(mint,ts,number(s.get('usdPrice')),number(s.get('liquidity')),number(s.get('holders')),number(s.get('volume24h'))))
        trend_history.save_snapshot(c,coin,now)
    c.commit()

def cycle(c):
    radar.run(c,ingest)

def collect(stop, interval):
    c = connect()
    failures = 0
    while not stop.is_set():
        start = time.monotonic()
        try:
            cycle(c)
            failures = 0
        except Exception as e:
            failures += 1
            state(c,'error',str(e))
            c.commit()
            print('FETCH ERROR',str(e),flush=True)
        stop.wait(max(interval-(time.monotonic()-start),min(900,30*2**min(failures,5)) if failures else 1))
    c.close()

def report():
    with closing(connect()) as c:
        return radar.report(c,time.time())

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        expected='http://127.0.0.1:'+str(self.server.server_port)
        if self.path!='/api/favorite' or self.headers.get('Origin')!=expected or self.headers.get('Host')!=expected[7:]:
            self.send_error(403);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=2048 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                raise ValueError('无效请求')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict) or type(data.get('enabled')) is not bool:
                raise ValueError('无效关注设定')
            with closing(connect()) as c:
                radar.setup(c)
                research.favorite(c,data.get('mint'),data['enabled'],time.time())
            body=b'{"ok":true}'
            self.send_response(200)
        except (ValueError,TypeError) as e:
            body=json.dumps({'error':str(e)},ensure_ascii=False).encode();self.send_response(400)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers();self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/api/social?'):
            try:
                mint=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('mint',[''])[0]
                with closing(connect()) as c:
                    result=social_links(c,mint)
                body=json.dumps(result,ensure_ascii=False).encode()
            except Exception:
                self.send_error(502, 'OTC social metadata unavailable');return
            mime='application/json; charset=utf-8'
        elif self.path == '/api/status':
            body=json.dumps(report(),ensure_ascii=False).encode()
            mime='application/json; charset=utf-8'
        elif self.path.startswith('/api/replay?id='):
            try:
                event_id=int(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['id'][0])
            except (ValueError,KeyError):
                self.send_error(400);return
            with closing(connect()) as c:
                radar.setup(c)
                result=research.replay(c,event_id,time.time())
            if result is None:
                self.send_error(404);return
            body=json.dumps(result,ensure_ascii=False).encode();mime='application/json; charset=utf-8'
        elif self.path in ('/','/index.html'):
            body=(ROOT/'index.html').read_bytes()
            mime='text/html; charset=utf-8'
        elif self.path in ('/app.js','/style.css','/icons.js','/otc-font.woff2','/otc-icon.svg'):
            body=(ROOT/self.path[1:]).read_bytes()
            mime={'js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8','woff2':'font/woff2','svg':'image/svg+xml'}[self.path.rsplit('.',1)[1]]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type',mime)
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args):
        pass

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--once',action='store_true')
    p.add_argument('--port',type=int,default=8765)
    p.add_argument('--interval',type=int,default=30)
    a=p.parse_args()
    if a.once:
        with connect() as c:
            cycle(c)
        print(json.dumps(report()['state']))
        return
    server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler)
    stop=threading.Event()
    worker=threading.Thread(target=collect,args=(stop,max(30,a.interval)),daemon=True)
    worker.start()
    print(f'http://127.0.0.1:{a.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop.set()
    finally:
        server.server_close()

if __name__=='__main__':
    main()
