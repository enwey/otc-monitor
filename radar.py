"""Evidence-based radar. Read-only market data, no wallet or trading functions."""
import hashlib
import json
import math
import statistics
import time
import urllib.parse
import urllib.request

from adaptive import get_state, put
import research

VERSION = research.VERSION
POOL_LIMIT = 90
CHART_BUDGET = 8
NEW_AGE = 48 * 3600


def num(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def setup(c):
    research.setup(c)
    c.executescript('''
    CREATE TABLE IF NOT EXISTS radar_pool(
      mint TEXT PRIMARY KEY, added REAL, expires REAL, cooldown REAL DEFAULT 0,
      reason TEXT, checked REAL DEFAULT 0, chart_checked REAL DEFAULT 0,
      pair TEXT, payload TEXT, candles TEXT, error TEXT, priority REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS radar_events(
      id INTEGER PRIMARY KEY, mint TEXT, opened REAL, updated REAL, phase TEXT,
      initial_price REAL, pair TEXT, evidence TEXT, closed REAL);
    CREATE INDEX IF NOT EXISTS radar_events_mint ON radar_events(mint,closed);
    CREATE TABLE IF NOT EXISTS radar_ranks(mint TEXT PRIMARY KEY, rank INTEGER);
    CREATE TABLE IF NOT EXISTS radar_observations(
      mint TEXT, ts REAL, pair TEXT, fingerprint TEXT, payload TEXT,
      PRIMARY KEY(mint,ts));
    CREATE TABLE IF NOT EXISTS radar_retries(mint TEXT PRIMARY KEY, attempts INTEGER, retry_at REAL);
    ''')


def choose_pairs(pairs, mints):
    """One deepest base-token pool per mint; never double-count overlapping pools."""
    chosen = {}
    for p in pairs:
        mint = (p.get('baseToken') or {}).get('address')
        if p.get('chainId') != 'solana' or mint not in mints:
            continue
        if not p.get('pairAddress') or not num(p.get('priceUsd')):
            continue
        depth = num((p.get('liquidity') or {}).get('usd')) or 0
        if depth > (num((chosen.get(mint, {}).get('liquidity') or {}).get('usd')) or -1):
            chosen[mint] = p
    return chosen


def candle_metrics(raw, now, minimum=15):
    # Only completed minute bars. Missing minutes are not filled with invented trades.
    bars = {}
    for r in raw:
        values = {k: num(r.get(k)) for k in ('time', 'open', 'high', 'low', 'close', 'volume')}
        if any(v is None for v in values.values()):
            continue
        if not now-7200 <= values['time'] <= now-60:
            continue
        if min(values[k] for k in ('open', 'high', 'low', 'close')) <= 0 or values['volume'] < 0:
            continue
        if values['low'] > min(values['open'], values['close']) or values['high'] < max(values['open'], values['close']):
            continue
        bars[values['time']] = values
    bars = sorted(bars.values(), key=lambda r: r['time'])[-90:]
    result = {'bars': bars, 'ready': False, 'at': bars[-1]['time']+60 if bars else None}
    if len(bars) < minimum or now-(result['at'] or 0) > 180:
        return result
    end = bars[-1]['time']
    recent = [r for r in bars if r['time'] >= end-120]
    base = [r for r in bars if end-1920 <= r['time'] < end-120]
    if len(recent) != 3 or len(base) < minimum-3:
        return result
    if any(b['time']-a['time'] != 60 for a, b in zip(bars[-minimum:], bars[-minimum+1:])):
        return result
    baseline = statistics.median(r['volume'] for r in base)
    mean_recent = sum(r['volume'] for r in recent)/3
    # Zero baseline remains undefined; it cannot yield an infinite "breakout" score.
    ratio = mean_recent / baseline if baseline > 0 else None
    gain = (recent[-1]['close']/recent[0]['open']-1)*100
    green = sum(r['close'] > r['open'] for r in recent)
    high = max(r['high'] for r in base)
    base_range = (max(r['high'] for r in base)/min(r['low'] for r in base)-1)*100
    result.update(ready=True, ratio=ratio, gain=gain, green=green,
                  breakout=recent[-1]['close'] > high,
                  dormant=base_range < 8 and ratio is not None and ratio >= 3,
                  drawdown=(recent[-1]['close']/max(r['high'] for r in recent)-1)*100,
                  baselineMinutes=len(base))
    return result


def analyze(pair, candles, created, now, observed_at, history=None, cohort=None, reference=None):
    """Scores evidence, never a probability. Null data is not zero activity."""
    history = history or []
    volume = pair.get('volume') or {}
    tx = (pair.get('txns') or {}).get('m5') or {}
    change = pair.get('priceChange') or {}
    buys, sells = num(tx.get('buys')), num(tx.get('sells'))
    v5, v1 = num(volume.get('m5')), num(volume.get('h1'))
    gain = num(change.get('m5'))
    depth = num((pair.get('liquidity') or {}).get('usd'))
    curve = depth is None and pair.get('dexId') == 'pumpfun'
    total = buys+sells if buys is not None and sells is not None else None
    share = buys/total if total and buys >= 0 and sells >= 0 else None
    # h1 includes m5: remove that overlap before comparing per-five-minute activity.
    age = now-created if created else None
    preceding = min(55,max(0,age/60-5)) if age is not None else 55
    baseline = (v1-v5)/(preceding/5) if preceding>=5 and v1 is not None and v5 is not None and v1 > v5 else None
    acceleration = v5/baseline if baseline and baseline > 0 else None
    cm = candle_metrics(candles, now)
    weak = False
    if not cm['ready'] and age is not None and 0 <= age < 3600:
        cm = candle_metrics(candles,now,6)
        weak = cm['ready']
    reasons, risks = [], []
    metrics = dict(volume5m=v5, buys=buys, sells=sells, buyShare=share,
                   gain5m=gain, liquidity=depth, volumeAcceleration=acceleration,
                   candleRatio=cm.get('ratio'), candleGain=cm.get('gain'),
                   chartAt=cm['at'], observedAt=observed_at,
                   price=num(pair.get('priceUsd')), pair=pair.get('pairAddress'))
    metrics['liquiditySource']='DEX Screener' if depth is not None else None
    if reference and 0<=now-reference['ts']<=300:
        metrics['referenceLiquidity']=reference['liquidity']
        metrics['referenceAt']=reference['ts']
    dex_fields = {k: metrics[k] for k in ('volume5m','buys','sells','gain5m','liquidity','price','pair')}
    metrics['dexSignature'] = hashlib.sha256(json.dumps(dex_fields, sort_keys=True).encode()).hexdigest()
    out = dict(phase='watch', score=0, reasons=reasons, risks=risks, metrics=metrics,
               bars=cm['bars'][-60:], version=VERSION, kind='new' if age is not None and age <= NEW_AGE else 'old')
    out['cohort']=cohort
    out['quality']='部分资料' if not cm['ready'] else '聚合证据可用'
    out['missing']=[]
    risks.append('买卖笔数不等于买卖金额；独立买家、关联钱包及合约权限未核验')
    risks.append('DEX 未提供行情生成时间；接收时间不代表交易即时性')
    if now-observed_at > 120:
        out['quality']='延迟，暂不采信'
        out['phase'] = 'delayed'; reasons.append('行情接收已超过 2 分钟'); return out
    if depth is None:
        risks.append('DEX 未提供池深；OTC 流动性仅作参考，不代表同池可成交深度')
    if v5 is not None and total is not None and v5>=0 and buys>=0 and sells>=0 and (v5<100 or total<3):
        out['phase']='inactive';out['quality']='低活跃，非接口错误'
        reasons.append('5 分钟无成交' if total==0 and v5==0 else '5 分钟成交额或交易笔数过低')
        return out
    required = (v5, total, share, gain)
    if any(x is None for x in required):
        out['quality']='必要栏位缺失'
        labels=dict(成交额=v5,买卖笔数=total,买入笔数比=share,价格变化=gain)
        out['phase'] = 'unavailable'; reasons.append('DEX 未提供：'+ '、'.join(k for k,v in labels.items() if v is None)); return out
    if depth is None and not curve:
        out['phase']='unavailable';out['quality']='池深未提供';reasons.append('DEX 未提供此交易池的流动性');return out
    if min(v5, total, depth if depth is not None else 0) < 0:
        out['phase'] = 'unavailable'; reasons.append('来源数值异常'); return out
    if depth is not None and depth < 5000:
        out['phase'] = 'risk'; reasons.append('流动性低于 $5,000'); return out
    if gain > 25:
        out['phase'] = 'risk'; reasons.append('5 分钟已急涨超过 25%，不列作早期讯号'); return out
    if gain < -8:
        out['phase'] = 'risk'; reasons.append('5 分钟回落超过 8%'); return out
    same_pool = [h for h in history if h['pair'] == pair.get('pairAddress') and now-h['ts'] <= 600]
    if same_pool:
        old_depth = num((json.loads(same_pool[0]['payload']).get('liquidity') or {}).get('usd'))
        if old_depth and depth is not None and depth < old_depth*.85:
            out['phase'] = 'risk'; reasons.append('同池流动性较近期观察下降超过 15%'); return out
    if not cm['ready']:
        out['phase'] = 'warming'; reasons.append('等待连续且新鲜的完整分钟 K 线'+('；Pump.fun 未提供池深' if curve else '')); return out
    minimum_volume=cohort['minimumVolume'] if cohort else 300
    active = v5 >= minimum_volume and total >= 12
    volume_up = (acceleration is not None and acceleration >= 1.8) or (cm.get('ratio') is not None and cm['ratio'] >= 2)
    buying = share >= .60
    constructive = -1 <= gain <= 15 and cm['gain'] > 0 and cm['green'] >= 2 and cm['drawdown'] > -5
    metrics['drawdown']=cm['drawdown']
    out['checks']=[dict(label='成交活跃',ok=active),dict(label='相对放量',ok=volume_up),
                   dict(label='买入笔数占优',ok=buying),dict(label='走势抬升',ok=constructive)]
    out['missing']=[x['label'] for x in out['checks'] if not x['ok']]
    for yes, label in [(active, '5 分钟成交额与交易笔数达到最低活跃门槛'),
                       (volume_up, '短周期成交量高于自身基准'),
                       (buying, '买入笔数占比至少 60%'),
                       (constructive, '完整分钟 K 线抬升，且未明显远离高点')]:
        if yes:
            reasons.append(label); out['score'] += 20
    if cm.get('breakout'):
        reasons.append('市值 K 线突破前段区间高点'); out['score'] += 10
    if not (active and volume_up and buying and constructive):
        if not reasons:
            reasons.append('暂无多项同步异动')
        return out
    out['phase'] = 'early'
    if weak:
        out['phase']='preview'
        out['quality']='短历史弱证据'
        reasons.insert(0,'新币已有 6 根完整分钟 K 线，尚未达正式确认历史长度')
        risks.append('新币弱证据预警，不可直接升级为动能确认')
    if age is not None and age > NEW_AGE and cm['dormant']:
        out['phase'] = 'revival'
        reasons.append('老币前段区间收窄，近期成交量显著放大')
    if curve:
        out['phase']='curve';out['quality']='动能可见，池深未核验'
        reasons.insert(0,'Pump.fun 价量异动；缺少池深，不升级正式确认')
    return out


def record(c, mint, evidence, now):
    current = c.execute('SELECT * FROM radar_events WHERE mint=? AND closed IS NULL ORDER BY id DESC LIMIT 1', (mint,)).fetchone()
    if current and json.loads(current['evidence']).get('version')!=evidence.get('version'):
        c.execute("UPDATE radar_events SET closed=?,phase='rule_changed' WHERE id=?",(now,current['id']))
        current=None
    phase = evidence['phase']
    positive = phase in ('curve','preview', 'early', 'revival', 'confirmed')
    if current and current['pair'] != evidence['metrics'].get('pair'):
        c.execute('UPDATE radar_events SET closed=?,updated=?,phase=? WHERE id=?', (now, now, 'pool_changed', current['id']))
        research.capture(c,mint,now)
        current = None
    if positive:
        if current:
            previous = json.loads(current['evidence'])
            old_at = previous['metrics'].get('chartAt') or 0
            new_at = evidence['metrics'].get('chartAt') or 0
            # Confirmation needs a later completed bar, not repeated HTTP responses.
            streak = previous.get('streak', 1)
            dex_changed = evidence['metrics'].get('sourceSignature',evidence['metrics'].get('dexSignature')) != previous['metrics'].get('sourceSignature',previous['metrics'].get('dexSignature'))
            if new_at > old_at and dex_changed:
                streak = streak+1 if new_at-old_at <= 180 else 1
            evidence['streak'] = streak
            if previous.get('phase') in ('preview','curve') or current['phase'] in ('preview','curve'):
                streak=1;evidence['streak']=1
            if phase not in ('preview','curve') and streak >= 2 and evidence['metrics']['gain5m'] >= 2:
                evidence['phase'] = 'confirmed'
                evidence['score'] = min(100, evidence['score']+10)
                if 'scoreParts' in evidence:
                    evidence['scoreParts'].append(dict(label='后续动能确认',ok=True,points=10))
                if current['phase']=='pullback':
                    evidence['reasons'].insert(0,'回落观察后再次形成同步增强证据')
            c.execute('UPDATE radar_events SET updated=?,phase=?,evidence=? WHERE id=?',
                      (now, evidence['phase'], json.dumps(evidence), current['id']))
        else:
            evidence['streak'] = 1
            c.execute('INSERT INTO radar_events(mint,opened,updated,phase,initial_price,pair,evidence) VALUES (?,?,?,?,?,?,?)',
                      (mint, now, now, phase, evidence['metrics']['price'], evidence['metrics']['pair'], json.dumps(evidence)))
    elif current:
        prior=json.loads(current['evidence'])
        pullback_at=prior.get('pullbackAt',now)
        m=evidence['metrics']
        if (phase=='watch' and current['phase'] in ('confirmed','pullback') and now-pullback_at<=180
                and m.get('gain5m') is not None and -5<=m['gain5m']<=15
                and m.get('drawdown') is not None and m['drawdown']>-5):
            evidence['phase']='pullback';evidence['pullbackAt']=pullback_at
            evidence['reasons'].insert(0,'确认后小幅回落，最多观察 3 分钟，尚未再次增强')
            c.execute('UPDATE radar_events SET updated=?,phase=?,evidence=? WHERE id=?',
                      (now,'pullback',json.dumps(evidence),current['id']))
            research.capture(c,mint,now)
            return
        # Source failure is not a demonstrated loss of market momentum.
        status = 'delayed' if phase in ('warming', 'unavailable', 'delayed') else 'faded'
        evidence['phase'] = status
        c.execute('UPDATE radar_events SET updated=?,closed=?,phase=?,evidence=? WHERE id=?',
                  (now, now, status, json.dumps(evidence), current['id']))
    research.capture(c,mint,now)


class Sources:
    def __init__(self):
        self.count = 0
        self.last = 0

    def get(self, url):
        time.sleep(max(0, .35-(time.monotonic()-self.last)))
        self.count += 1
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'OTCRadar/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as response:
                return json.load(response)
        finally:
            self.last = time.monotonic()

    def coins(self, page=0, sort='new'):
        data = self.get('https://otcdesks.cash/api/coins?' + urllib.parse.urlencode(dict(page=page, size=60, sort=sort, dir='desc')))
        if not isinstance(data.get('coins'), list):
            raise ValueError('OTC coin schema changed')
        return data

    def pairs(self, mints):
        raise RuntimeError('Legacy external collection disabled; use otc_radar')

    def chart(self, mint):
        data = self.get('https://otcdesks.cash/api/chart?' + urllib.parse.urlencode(dict(mint=mint, interval='1_MINUTE', candles=90, type='mcap')))
        if not isinstance(data.get('candles'), list):
            raise ValueError('OTC chart schema changed')
        return data['candles']


def seed(c, coin, now, reason):
    s = coin.get('snapshot') or {}
    if (num(s.get('liquidity')) or 0) < 2500 or (num(s.get('holders')) or 0) < 5:
        return
    if now-(num(s.get('at')) or 0) > 600:
        return
    existing = c.execute('SELECT * FROM radar_pool WHERE mint=?', (coin['mint'],)).fetchone()
    if existing and (existing['expires'] > now or existing['cooldown'] > now):
        return
    c.execute('''INSERT OR REPLACE INTO radar_pool(mint,added,expires,reason)
                 VALUES (?,?,?,?)''', (coin['mint'], now, now+900, reason))


def run(c, ingest, sources=None):
    setup(c)
    src = sources or Sources()
    start = time.time()
    errors = []
    c.execute('UPDATE radar_pool SET expires=max(expires,?) WHERE mint IN (SELECT mint FROM favorites)',(start+900,))
    if start-get_state(c, 'radar_new_at', 0) >= 60:
        watermark = get_state(c, 'radar_watermark', 0)
        newest = watermark
        gap = False
        try:
            for page in range(3):
                c.commit()
                data = src.coins(page)
                ingest(c, data)
                put(c, 'total', data.get('total'))
                timestamps = [num(x.get('createdAt')) or 0 for x in data['coins']]
                newest = max([newest]+timestamps)
                for coin in data['coins']:
                    if 0 <= start-(num(coin.get('createdAt')) or 0) <= NEW_AGE:
                        seed(c, coin, start, '近期发行')
                if not timestamps or len(timestamps) < 60 or min(timestamps) <= max(watermark, start-NEW_AGE):
                    break
                gap = page == 2 and watermark > 0
            put(c, 'radar_watermark', newest)
            put(c, 'radar_new_at', time.time())
            put(c, 'discoveryGap', gap)
        except Exception as e:
            errors.append('新币来源：'+str(e))
    if start-get_state(c, 'radar_board_at', 0) >= 300:
        try:
            previous = {r['mint']: r['rank'] for r in c.execute('SELECT * FROM radar_ranks')}
            ranks = {}
            for page in range(3):
                c.commit()
                data = src.coins(page, 'volume24h')
                ingest(c, data)
                for index, coin in enumerate(data['coins']):
                    mint = coin['mint']; rank = page*60+index+1
                    ranks[mint] = rank
                    old = previous.get(mint)
                    rows = c.execute('SELECT * FROM samples WHERE mint=? AND ts>=? ORDER BY ts', (mint, start-900)).fetchall()
                    motion = len(rows) >= 2 and rows[0]['price'] and rows[-1]['price'] and rows[-1]['price'] > rows[0]['price']*1.015
                    event = old is None or old-rank >= max(5, old*.2)
                    # Bootstrap probes at most the first 30, not all old coins.
                    if (previous and (event or motion)) or (not previous and rank <= 30):
                        seed(c, coin, start, '榜内价格异动' if motion else '活跃榜入围／升位' if previous else '首次活跃样本')
            c.execute('DELETE FROM radar_ranks')
            c.executemany('INSERT INTO radar_ranks VALUES (?,?)', ranks.items())
            put(c, 'radar_board_at', time.time())
        except Exception as e:
            errors.append('活跃榜来源：'+str(e))
    pool = c.execute('''SELECT * FROM radar_pool WHERE expires>? ORDER BY
        (mint IN (SELECT mint FROM favorites)) DESC,priority DESC,added DESC''', (start,)).fetchall()
    for row in pool[POOL_LIMIT:]:
        c.execute('UPDATE radar_pool SET expires=?,cooldown=? WHERE mint=?', (start, start+3600, row['mint']))
    pool = pool[:POOL_LIMIT]
    fetched_pairs={}
    for offset in range(0, len(pool), 30):
        batch = pool[offset:offset+30]
        try:
            c.commit()
            pairs = src.pairs([r['mint'] for r in batch])
            fetched_pairs.update(pairs)
            now = time.time()
            for row in batch:
                mint = row['mint']; p = pairs.get(mint)
                if not p:
                    c.execute('UPDATE radar_pool SET checked=?,error=? WHERE mint=?', (now, 'DEX 无可用交易池', mint))
                    continue
                payload = json.dumps(p, sort_keys=True)
                fingerprint = hashlib.sha256(payload.encode()).hexdigest()
                gain = num((p.get('priceChange') or {}).get('m5')) or 0
                tx = (p.get('txns') or {}).get('m5') or {}
                buys = num(tx.get('buys')) or 0
                depth = num((p.get('liquidity') or {}).get('usd'))
                volume5 = num((p.get('volume') or {}).get('m5'))
                eligible = (depth is not None and depth >= 5000 or depth is None and p.get('dexId')=='pumpfun') and volume5 is not None and volume5 >= 100 and -8 <= gain <= 25
                priority = (20 if -1 <= gain <= 15 else 10)+min(20, buys) if eligible else 0
                # Pool switch discards the old chart and evidence continuity.
                if row['pair'] and row['pair'] != p['pairAddress']:
                    c.execute('UPDATE radar_pool SET candles=NULL,chart_checked=0 WHERE mint=?', (mint,))
                c.execute("UPDATE radar_pool SET checked=?,pair=?,payload=?,error=CASE WHEN error LIKE 'K 线%' THEN error ELSE NULL END,priority=? WHERE mint=?",
                          (now, p['pairAddress'], payload, priority, mint))
                previous = c.execute('SELECT fingerprint FROM radar_observations WHERE mint=? ORDER BY ts DESC LIMIT 1', (mint,)).fetchone()
                if not previous or previous[0] != fingerprint:
                    c.execute('INSERT OR IGNORE INTO radar_observations VALUES (?,?,?,?,?)', (mint, now, p['pairAddress'], fingerprint, payload))
        except Exception as e:
            errors.append('DEX 来源：'+str(e))
            for row in batch:
                c.execute('UPDATE radar_pool SET error=? WHERE mint=?', ('DEX 来源暂不可用：'+str(e), row['mint']))
    # Bounded, fair chart queue: actionable evidence first, but age prevents starvation.
    chart_queue = c.execute('''SELECT p.* FROM radar_pool p LEFT JOIN radar_retries r ON p.mint=r.mint
        WHERE p.expires>? AND p.payload IS NOT NULL AND p.priority>0 AND coalesce(r.retry_at,0)<=?
        AND p.chart_checked<? ORDER BY (p.priority + min(60, (? - p.chart_checked)/10)) DESC,p.chart_checked ASC LIMIT ?''',
        (start, start, start-45, start, CHART_BUDGET)).fetchall()
    for row in chart_queue:
        try:
            c.commit()
            candles = src.chart(row['mint'])
            c.execute("UPDATE radar_pool SET candles=?,chart_checked=?,error=CASE WHEN error LIKE 'K 线%' THEN NULL ELSE error END WHERE mint=?",
                      (json.dumps(candles), time.time(), row['mint']))
            c.execute('DELETE FROM radar_retries WHERE mint=?', (row['mint'],))
        except Exception as e:
            c.execute('UPDATE radar_pool SET chart_checked=?,error=? WHERE mint=?', (time.time(), 'K 线暂不可用：'+str(e), row['mint']))
            previous = c.execute('SELECT attempts FROM radar_retries WHERE mint=?', (row['mint'],)).fetchone()
            attempts = previous[0]+1 if previous else 1
            c.execute('INSERT OR REPLACE INTO radar_retries VALUES (?,?,?)',
                      (row['mint'], attempts, time.time()+min(900,60*2**min(attempts,4))))
            errors.append('K 线来源：'+str(e))
    now = time.time()
    cohorts=research.cohort_context(c,now)
    for row in c.execute('''SELECT p.*,c.created FROM radar_pool p JOIN coins c ON c.mint=p.mint WHERE p.expires>?''', (start,)).fetchall():
        if not row['payload']:
            continue
        history = c.execute('SELECT * FROM radar_observations WHERE mint=? AND ts>=? ORDER BY ts', (row['mint'], now-600)).fetchall()
        reference=c.execute('SELECT ts,liquidity FROM samples WHERE mint=? ORDER BY ts DESC LIMIT 1',(row['mint'],)).fetchone()
        evidence = analyze(json.loads(row['payload']), json.loads(row['candles'] or '[]'), row['created'], now, row['checked'], history,cohorts.get(row['mint']),reference)
        if row['error'] and not (row['error'].startswith('K 线') and evidence['phase']=='inactive'):
            evidence['phase'] = 'unavailable'
            evidence['quality']='来源异常'
            evidence['reasons'] = [row['error']]
        record(c, row['mint'], evidence, now)
        if evidence['phase'] in ('curve','preview','early', 'revival', 'confirmed','pullback'):
            c.execute('UPDATE radar_pool SET expires=?,priority=? WHERE mint=?', (now+900, evidence['score'], row['mint']))
        elif evidence['phase'] == 'risk':
            c.execute('UPDATE radar_pool SET expires=?,cooldown=? WHERE mint=?', (now, now+3600, row['mint']))
        elif evidence['phase']=='inactive' and now-row['added']>=180 and not c.execute('SELECT 1 FROM favorites WHERE mint=?',(row['mint'],)).fetchone():
            c.execute('UPDATE radar_pool SET expires=?,cooldown=? WHERE mint=?',(now,now+3600,row['mint']))
    # Expired/capacity-removed candidates cannot leave a live signal behind.
    expired=c.execute('''SELECT mint FROM radar_events WHERE closed IS NULL AND mint IN
                        (SELECT mint FROM radar_pool WHERE expires<=?)''',(now,)).fetchall()
    c.execute('''UPDATE radar_events SET closed=?,updated=?,phase='expired' WHERE closed IS NULL AND mint IN
                 (SELECT mint FROM radar_pool WHERE expires<=?)''', (now, now, now))
    for item in expired:research.capture(c,item['mint'],now)
    c.execute('UPDATE radar_pool SET cooldown=max(cooldown,expires+3600) WHERE expires<=?', (now,))
    followups=research.targets(c,now)
    missing=list(dict.fromkeys(e['mint'] for e in followups if e['mint'] not in fetched_pairs))
    for offset in range(0,min(60,len(missing)),30):
        try:
            c.commit()
            fetched_pairs.update(src.pairs(missing[offset:offset+30]))
        except Exception as e:
            errors.append('后续观测：'+str(e))
    for event in followups:
        research.measure(c,event,fetched_pairs.get(event['mint']),time.time())
    c.execute('DELETE FROM radar_observations WHERE ts<?', (now-86400,))
    c.execute('DELETE FROM samples WHERE ts<?', (now-7*86400,))
    put(c, 'radar', dict(version=VERSION, at=now, requests=src.count, chartChecks=len(chart_queue),
                        pool=len(pool), limit=POOL_LIMIT, seconds=round(now-start,1),
                        errors=errors[:6], sourceMode='snapshot+minute-candles', interval=30))
    put(c, 'error', None)
    c.commit()


def report(c, now):
    setup(c)
    live, archive, watch = [], [], []
    favorite_mints=[r['mint'] for r in c.execute('SELECT mint FROM favorites ORDER BY added')]
    cohorts=research.cohort_context(c,now)
    for row in c.execute('''SELECT e.*,c.name,c.symbol,c.created FROM radar_events e JOIN coins c ON c.mint=e.mint
                            ORDER BY e.updated DESC LIMIT 250'''):
        item = dict(row)
        item['evidence'] = json.loads(item['evidence'])
        if not item['closed'] and now-item['updated'] > 120:
            item['phase'] = 'delayed'
            item['evidence']['quality']='延迟，暂不采信'
        price = item['evidence']['metrics'].get('price')
        item['since'] = (price/item['initial_price']-1)*100 if price and item['initial_price'] else None
        item['outcomes']=research.outcome_rows(c,item,now)
        (archive if item['closed'] else live).append(item)
    for row in c.execute('''SELECT p.*,c.name,c.symbol,c.created FROM radar_pool p JOIN coins c ON c.mint=p.mint
                            WHERE p.expires>? OR p.mint IN (SELECT mint FROM favorites) ORDER BY p.priority DESC''', (now,)):
        item = dict(row)
        if item['payload']:
            history=c.execute('SELECT * FROM radar_observations WHERE mint=? AND ts>=? ORDER BY ts',(item['mint'],now-600)).fetchall()
            reference=c.execute('SELECT ts,liquidity FROM samples WHERE mint=? ORDER BY ts DESC LIMIT 1',(item['mint'],)).fetchone()
            ev = analyze(json.loads(item['payload']), json.loads(item['candles'] or '[]'), item['created'], now, item['checked'],history,cohorts.get(item['mint']),reference)
        else:
            ev = {'phase':'unavailable', 'score':0, 'metrics':{}, 'reasons':[item['error'] or '等待交易池资料'], 'risks':[], 'bars':[]}
        if item['error'] and not (item['error'].startswith('K 线') and ev['phase']=='inactive'):
            ev['phase'] = 'unavailable'; ev['quality']='来源异常'; ev['reasons'] = [item['error']]
        watch.append(dict(mint=item['mint'], name=item['name'], symbol=item['symbol'], reason=item['reason'],
                          phase=ev['phase'], evidence=ev, opened=item['added'], since=None))
    return dict(now=now, live=live, archive=archive, watch=watch,
                favorites=[r for r in watch if r['mint'] in favorite_mints],favoriteMints=favorite_mints,
                quality=research.quality(c,now),
                state={r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM state')},
                capabilities=dict(trades='5 分钟聚合笔数', buyers=False, netFlow=False, stream=False))
