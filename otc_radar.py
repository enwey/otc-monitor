"""OTC-only collection and source-native evidence rules."""
import hashlib
import json
import time
import urllib.parse
import urllib.request

import radar as legacy
import research
import trend_history
from adaptive import get_state, put

VERSION = 'otc-2.0'
POOL_LIMIT = 90
SNAPSHOT_MAX_AGE = 600


class Sources:
    def __init__(self):
        self.count = 0
        self.cache = {}

    def get(self, path, **params):
        if path not in ('/api/coins', '/api/chart'):
            raise ValueError('Only OTC market endpoints are permitted')
        url = 'https://otcdesks.cash' + path + '?' + urllib.parse.urlencode(params)
        self.count += 1
        req = urllib.request.Request(url, headers={'Accept':'application/json','User-Agent':'OTCRadar/2.0'})
        # Refuse redirects: a public endpoint must not redirect collection off OTC.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            return json.load(response)

    def coins(self, page=0, sort='new', mint=None):
        params = dict(mint=mint) if mint else dict(page=page,size=60,sort=sort,dir='desc')
        data = self.get('/api/coins', **params)
        if not isinstance(data.get('coins'), list):
            raise ValueError('OTC coins schema changed')
        for coin in data['coins']:
            self.cache[coin['mint']] = coin
        return data

    def chart(self, mint):
        data = self.get('/api/chart',mint=mint,interval='1_MINUTE',candles=90,type='mcap')
        if not isinstance(data.get('candles'), list):
            raise ValueError('OTC chart schema changed')
        return data['candles']


def setup(c):
    legacy.setup(c)
    trend_history.setup(c)
    if get_state(c,'source_version',None) != VERSION:
        now=time.time()
        c.execute("UPDATE radar_events SET closed=?,updated=?,phase='rule_changed' WHERE closed IS NULL",(now,now))
        # Keep historical events, but never analyze legacy DEX payloads as OTC data.
        c.execute('UPDATE radar_pool SET payload=NULL,candles=NULL,pair=NULL,error=NULL,checked=0,chart_checked=0,priority=0')
        c.execute('DELETE FROM radar_retries')
        put(c,'source_version',VERSION)
        c.commit()
    if get_state(c,'history_version',None) != trend_history.VERSION:
        now=time.time()
        c.execute("UPDATE radar_events SET closed=?,updated=?,phase='rule_changed' WHERE closed IS NULL",(now,now))
        for row in c.execute('SELECT mint,candles FROM radar_pool WHERE candles IS NOT NULL').fetchall():
            trend_history.save_bars(c,row['mint'],json.loads(row['candles']),now)
        put(c,'history_version',trend_history.VERSION)
        c.commit()


def analyze(coin, candles, now, received, history=()):
    n=legacy.num
    s=coin.get('snapshot') or {}
    at=n(s.get('at'));depth=n(s.get('liquidity'));holders=n(s.get('holders'))
    created=n(coin.get('createdAt'));age=now-created if created else None
    cm=legacy.candle_metrics(candles,now)
    weak=False
    if not cm['ready'] and age is not None and 0<=age<3600:
        cm=legacy.candle_metrics(candles,now,6);weak=cm['ready']
    bars=cm['bars'];five=bars[-5:]
    contiguous=len(five)==5 and all(b['time']-a['time']==60 for a,b in zip(five,five[1:]))
    gain=(five[-1]['close']/five[0]['open']-1)*100 if contiguous else None
    volume=sum(b['volume'] for b in five) if contiguous else None
    previous=[r for r in history if at and 240<=at-r['ts']<=900]
    first=min(previous,key=lambda r:abs(at-r['ts']-300)) if previous else None
    added=holders-first['holders'] if first and holders is not None and first['holders'] is not None else None
    depth_change=(depth/first['liquidity']-1)*100 if first and depth is not None and first['liquidity'] else None
    metrics=dict(price=n(s.get('usdPrice')),liquidity=depth,holders=holders,holdersAdded=added,
        holderWindow=(at-first['ts'])/60 if first else None,liquidityChange=depth_change,
        gain5m=gain,volume5m=volume,candleRatio=cm.get('ratio'),chartAt=cm['at'],
        observedAt=received,snapshotAt=at,pair='otc:'+coin['mint'],source='OTC',
        liquiditySource='OTC snapshot',volumeUnit='OTC 原始单位（未核实）',drawdown=cm.get('drawdown'))
    metrics['sourceSignature']=hashlib.sha256(json.dumps([s,cm['at'],five],sort_keys=True).encode()).hexdigest()
    e=dict(phase='watch',score=0,quality='OTC 部分资料',metrics=metrics,bars=bars[-60:],
        reasons=[],risks=['OTC 平台流动性不是已核验同池深度','K 线为市值；成交量单位未核实，不标示美元','未提供买卖笔数、独立买家及合约风险验证'],missing=[],version=VERSION)
    def stop(phase,reason,quality):
        e.update(phase=phase,quality=quality);e['reasons'].append(reason);return e
    if at is None or any(metrics[k] is None for k in ('price','liquidity','holders')):
        return stop('unavailable','OTC 快照缺少价格、流动性、持有人或时间戳','快照不完整')
    if at>now+60 or now-at>SNAPSHOT_MAX_AGE or now-received>120:
        return stop('delayed','OTC 快照超过 10 分钟、时间异常或接收超过 2 分钟','资料延迟')
    if metrics['price']<=0 or depth<0 or holders<0:
        return stop('unavailable','OTC 快照数值无效','来源数值异常')
    minimum=6 if weak else 15
    if not bars:
        chart_reason='OTC 尚未返回有效完整分钟 K 线'
    elif now-(cm['at'] or 0)>180:
        chart_reason=f"最新 K 线已滞后 {int(now-cm['at'])} 秒（上限 180 秒）"
    elif len(bars)<minimum:
        chart_reason=f'有效 K 线 {len(bars)} 根，至少需要 {minimum} 根'
    else:
        chart_reason=f'最近 {minimum} 根分钟 K 线不连续，缺口不能当作零成交'
    chart_ok=cm['ready'] and contiguous
    e['scoreParts']=[
        dict(label='相对放量',points=20,ok=(cm['ratio']>=2 if cm.get('ratio') is not None else None) if chart_ok else None,reason=chart_reason if not chart_ok else '成交量基准为零，无法计算倍数'),
        dict(label='市值抬升',points=20,ok=(-1<=gain<=15 and cm['gain']>0 and cm['green']>=2 and cm['drawdown']>-5) if chart_ok else None,reason=chart_reason),
        dict(label='持有人增长',points=20,ok=added>=3 and holders>=20 if added is not None else None,reason='等待相差 4～15 分钟的持有人快照'),
        dict(label='流动性稳定',points=20,ok=depth>=5000 and depth_change>=-5 if depth_change is not None else None,reason='等待相差 4～15 分钟的流动性快照'),
        dict(label='区间突破',points=10,ok=bool(cm.get('breakout')) if chart_ok else None,reason=chart_reason)]
    e['partialScore']=sum(p['points'] for p in e['scoreParts'] if p['ok'] is True)
    if depth<5000 or (depth_change is not None and depth_change < -15):
        return stop('risk','OTC 流动性低于 $5,000 或较基准下降超过 15%','流动性风险')
    if not cm['ready'] or not contiguous:
        return stop('warming',chart_reason,'K 线尚不可判断')
    if gain>25 or gain < -8:
        return stop('risk','市值五分钟已过热或急跌','走势风险')
    if volume==0:
        return stop('inactive','五根完整分钟 K 线成交量为零','低活跃')
    checks=[('相对放量',cm.get('ratio') is not None and cm['ratio']>=2),
        ('市值抬升',-1<=gain<=15 and cm['gain']>0 and cm['green']>=2 and cm['drawdown']>-5),
        ('持有人增长',added is not None and added>=3 and holders>=20),
        ('流动性稳定',depth_change is not None and depth_change>=-5)]
    e['checks']=[dict(label=k,ok=v) for k,v in checks]
    e['missing']=[k for k,v in checks if not v]
    e['reasons']=[k for k,v in checks if v] or ['暂无同步动能']
    e['score']=20*sum(v for k,v in checks)+10*bool(cm.get('breakout'))
    e['quality']='OTC 快照与市值 K 线可判断'
    if all(v for k,v in checks):
        e['phase']='preview' if weak else 'revival' if age is not None and age>172800 and cm['dormant'] else 'early'
        if weak:e['quality']='短历史弱证据'
    elif first is None:
        e['quality']='累积持有人／流动性基准'
    return e


def evidence(c,row,now):
    if not row['payload']:
        return dict(phase='unavailable',score=0,quality='等待 OTC',metrics={},bars=[],reasons=[row['error'] or '等待 OTC 快照'],risks=[],version=VERSION)
    coin=json.loads(row['payload'])
    hist=c.execute('SELECT * FROM samples WHERE mint=? ORDER BY ts',(row['mint'],)).fetchall()
    e=analyze(coin,json.loads(row['candles'] or '[]'),now,row['checked'],hist)
    if row['error'] and e['phase']=='risk' and row['error'].startswith('K 线'):
        e['risks'].append(row['error'])
    elif row['error']:
        e.update(phase='unavailable',quality='OTC 来源异常',reasons=[row['error']])
        if row['error'].startswith(('K 线','K 線')):
            for p in e.get('scoreParts',[]):
                if p['label'] in ('相对放量','市值抬升','区间突破'):
                    p.update(ok=None,reason=row['error'])
        else:
            for p in e.get('scoreParts',[]):p.update(ok=None,reason=row['error'])
        e['partialScore']=sum(p['points'] for p in e.get('scoreParts',[]) if p['ok'] is True)
        e['score']=0
    trend_history.enrich(c,row['mint'],e,coin,now)
    e['version']=VERSION+'+'+trend_history.VERSION
    return e


def run(c,ingest,sources=None):
    setup(c);src=sources or Sources();start=time.time();errors=[]
    c.execute('UPDATE radar_pool SET expires=max(expires,?) WHERE mint IN (SELECT mint FROM favorites)',(start+900,))
    for key,period,sort in [('radar_new_at',60,'new'),('radar_board_at',300,'volume24h')]:
        if start-get_state(c,key,0)<period:continue
        try:
            previous={r['mint']:r['rank'] for r in c.execute('SELECT * FROM radar_ranks')};ranks={}
            watermark=get_state(c,'radar_watermark',0);newest=watermark;gap=False
            for page in range(3):
                c.commit();data=src.coins(page,sort);ingest(c,data)
                for i,coin in enumerate(data['coins']):
                    created=legacy.num(coin.get('createdAt')) or 0;newest=max(newest,created)
                    rank=page*60+i+1;ranks[coin['mint']]=rank;old=previous.get(coin['mint'])
                    h=c.execute('SELECT * FROM samples WHERE mint=? AND ts>=? ORDER BY ts',(coin['mint'],start-900)).fetchall()
                    motion=len(h)>=2 and h[0]['price'] and h[-1]['price'] and h[-1]['price']>h[0]['price']*1.015
                    eligible=0<=start-created<=172800 if sort=='new' else ((old is None or old-rank>=max(5,old*.2) or motion) if previous else rank<=30)
                    if eligible:legacy.seed(c,coin,start,'OTC 近期发行' if sort=='new' else 'OTC 活跃榜异动')
                times=[legacy.num(x.get('createdAt')) or 0 for x in data['coins']]
                if len(data['coins'])<60:break
                if sort=='new' and times and min(times)<=max(watermark,start-172800):break
                if sort=='new' and page==2:gap=True
            if sort=='volume24h':
                c.execute('DELETE FROM radar_ranks');c.executemany('INSERT INTO radar_ranks VALUES (?,?)',ranks.items())
            else:put(c,'radar_watermark',newest);put(c,'discoveryGap',gap)
            put(c,key,time.time())
        except Exception as ex:errors.append('OTC '+sort+'：'+str(ex))
    pool=c.execute('SELECT * FROM radar_pool WHERE expires>? ORDER BY (mint IN (SELECT mint FROM favorites)) DESC,priority DESC,added DESC',(start,)).fetchall()
    for r in pool[POOL_LIMIT:]:c.execute('UPDATE radar_pool SET expires=?,cooldown=? WHERE mint=?',(start,start+3600,r['mint']))
    pool=pool[:POOL_LIMIT]
    # Bulk discovery responses refresh matches without extra per-token requests.
    budget=8
    for row in sorted(pool,key=lambda r:r['checked']):
        mint=row['mint'];coin=src.cache.get(mint)
        if coin is None and start-row['checked']>=60 and budget:
            budget-=1
            try:
                c.commit();d=src.coins(mint=mint);ingest(c,d);coin=src.cache.get(mint)
                if coin is None:raise ValueError('OTC 未返回此合约')
            except Exception as ex:
                c.execute('UPDATE radar_pool SET checked=?,error=? WHERE mint=?',(time.time(),'OTC 快照：'+str(ex),mint));continue
        if coin:
            c.execute("UPDATE radar_pool SET payload=?,pair=?,checked=?,error=CASE WHEN error LIKE 'K 线%' THEN error ELSE NULL END WHERE mint=?",(json.dumps(coin),'otc:'+mint,time.time(),mint))
    queue=c.execute('''SELECT p.* FROM radar_pool p LEFT JOIN radar_retries r ON r.mint=p.mint
        WHERE p.expires>? AND p.payload IS NOT NULL AND p.chart_checked<? AND coalesce(r.retry_at,0)<=?
        ORDER BY p.chart_checked ASC,p.priority DESC LIMIT 8''',(start,start-60,start)).fetchall()
    for row in queue:
        snapshot=json.loads(row['payload']).get('snapshot') or {}
        depth=legacy.num(snapshot.get('liquidity'))
        if depth is not None and depth<5000:
            continue
        try:
            c.commit();bars=src.chart(row['mint'])
            trend_history.save_bars(c,row['mint'],bars,time.time())
            c.execute("UPDATE radar_pool SET candles=?,chart_checked=?,error=CASE WHEN error LIKE 'K 线%' THEN NULL ELSE error END WHERE mint=?",(json.dumps(bars),time.time(),row['mint']))
            c.execute('DELETE FROM radar_retries WHERE mint=?',(row['mint'],))
        except Exception as ex:
            old=c.execute('SELECT attempts FROM radar_retries WHERE mint=?',(row['mint'],)).fetchone();attempt=(old[0] if old else 0)+1
            c.execute('INSERT OR REPLACE INTO radar_retries VALUES (?,?,?)',(row['mint'],attempt,time.time()+min(900,60*2**min(attempt,4))))
            c.execute('UPDATE radar_pool SET chart_checked=?,error=? WHERE mint=?',(time.time(),'K 线 OTC：'+str(ex),row['mint']))
            errors.append('OTC K 线：'+str(ex))
    now=time.time()
    for row in c.execute('SELECT * FROM radar_pool WHERE expires>?',(now,)).fetchall():
        e=evidence(c,row,now);legacy.record(c,row['mint'],e,now)
        if e['phase'] in ('early','preview','revival','confirmed','pullback'):
            c.execute('UPDATE radar_pool SET expires=?,priority=? WHERE mint=?',(now+900,e['score'],row['mint']))
        elif e['phase'] in ('risk','inactive') and not c.execute('SELECT 1 FROM favorites WHERE mint=?',(row['mint'],)).fetchone():
            c.execute('UPDATE radar_pool SET expires=?,cooldown=? WHERE mint=?',(now,now+3600,row['mint']))
    c.execute("UPDATE radar_events SET closed=?,updated=?,phase='expired' WHERE closed IS NULL AND mint IN (SELECT mint FROM radar_pool WHERE expires<=?)",(now,now,now))
    for r in c.execute("SELECT mint FROM radar_events WHERE closed=? AND phase='expired'",(now,)).fetchall():
        research.capture(c,r['mint'],now)
    c.execute('UPDATE radar_pool SET cooldown=max(cooldown,expires+3600) WHERE expires<=?',(now,))
    # Follow-ups use fresh OTC source timestamps within the target window only.
    extra=2
    for event in research.targets(c,now):
        if not (event['pair'] or '').startswith('otc:'):continue
        coin=src.cache.get(event['mint'])
        if coin is None and extra:
            extra-=1
            try:c.commit();ingest(c,src.coins(mint=event['mint']));coin=src.cache.get(event['mint'])
            except Exception as ex:errors.append('OTC 后续观测：'+str(ex))
        if not coin:continue
        s=coin.get('snapshot') or {};at=legacy.num(s.get('at'));price=legacy.num(s.get('usdPrice'))
        for h in research.HORIZONS:
            due=event['opened']+h
            if at and price and price>0 and due<=at<=min(now,due+120) and due<=now<=due+120 and event['initial_price']:
                c.execute('INSERT OR IGNORE INTO event_outcomes VALUES (?,?,?,?,?,?)',(event['id'],h,at,price,(price/event['initial_price']-1)*100,'observed'))
    put(c,'radar',dict(version=VERSION,at=now,requests=src.count,chartChecks=len(queue),pool=len(pool),limit=90,seconds=round(now-start,1),errors=errors[:6],sourceMode='OTC only',interval=30))
    put(c,'error',None)
    c.execute('DELETE FROM samples WHERE ts<?',(now-7*86400,));c.commit()
    if now-get_state(c,'history_cleanup',0)>3600:
        c.execute('DELETE FROM otc_bars WHERE time<?',(now-trend_history.RETENTION,))
        c.execute('DELETE FROM otc_snapshots WHERE ts<?',(now-trend_history.RETENTION,))
        put(c,'history_cleanup',now);c.commit()


def report(c,now):
    setup(c);live=[];archive=[];watch=[]
    favorites=[r['mint'] for r in c.execute('SELECT mint FROM favorites')]
    for row in c.execute('SELECT e.*,c.name,c.symbol FROM radar_events e JOIN coins c ON c.mint=e.mint ORDER BY e.updated DESC LIMIT 250'):
        r=dict(row);r['evidence']=json.loads(r['evidence']);p=r['evidence']['metrics'].get('price')
        r['since']=(p/r['initial_price']-1)*100 if p and r['initial_price'] else None
        if not r['closed'] and now-r['updated']>120:r['phase']='delayed'
        (archive if r['closed'] else live).append(r)
    for row in c.execute('SELECT p.*,c.name,c.symbol FROM radar_pool p JOIN coins c ON c.mint=p.mint WHERE p.expires>? OR p.mint IN (SELECT mint FROM favorites) ORDER BY p.priority DESC',(now,)):
        e=evidence(c,row,now)
        watch.append(dict(mint=row['mint'],name=row['name'],symbol=row['symbol'],phase=e['phase'],evidence=e,opened=row['added'],since=None))
    quality=research.quality(c,now)
    quality['paths']=trend_history.path_outcomes(c,now)
    return dict(now=now,live=live,archive=archive,watch=watch,favorites=[r for r in watch if r['mint'] in favorites],favoriteMints=favorites,quality=quality,state={r['key']:json.loads(r['value']) for r in c.execute("SELECT * FROM state WHERE key NOT LIKE 'social:%'")},capabilities=dict(source='OTC only',buyers=False,netFlow=False,stream=False))
