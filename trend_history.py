"""OTC-native history and transparent, heuristic trend measurements."""
import math
import statistics

VERSION = 'history-1.0'
RETENTION = 30 * 86400


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS otc_bars(
      mint TEXT,time INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,
      received REAL,PRIMARY KEY(mint,time));
    CREATE INDEX IF NOT EXISTS otc_bars_time ON otc_bars(time);
    CREATE TABLE IF NOT EXISTS otc_snapshots(
      mint TEXT,ts REAL,holders REAL,liquidity REAL,marketcap REAL,received REAL,
      PRIMARY KEY(mint,ts));
    CREATE INDEX IF NOT EXISTS otc_snapshots_time ON otc_snapshots(ts);
    ''')


def valid_bars(raw, now):
    result = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        b = {k:number(row.get(k)) for k in ('time','open','high','low','close','volume')}
        if any(v is None for v in b.values()):
            continue
        if not now-RETENTION <= b['time'] <= now-60 or b['time'] % 60:
            continue
        if min(b[k] for k in ('open','high','low','close')) <= 0 or b['volume'] < 0:
            continue
        if b['low'] > min(b['open'],b['close']) or b['high'] < max(b['open'],b['close']):
            continue
        result[b['time']] = b
    return sorted(result.values(), key=lambda b:b['time'])


def save_bars(c, mint, raw, now):
    # First observed closed bar is immutable; later responses cannot rewrite replay evidence.
    c.executemany('INSERT OR IGNORE INTO otc_bars VALUES (?,?,?,?,?,?,?,?)',
        [(mint,b['time'],b['open'],b['high'],b['low'],b['close'],b['volume'],now)
         for b in valid_bars(raw,now)])


def save_snapshot(c, coin, now):
    s=coin.get('snapshot') or {}
    ts=number(s.get('at'))
    if ts is None or not now-RETENTION<=ts<=now:
        return
    values=[number(s.get(k)) for k in ('holders','liquidity','marketCap')]
    values=[v if v is not None and v>=0 else None for v in values]
    c.execute('INSERT OR IGNORE INTO otc_snapshots VALUES (?,?,?,?,?,?)',
              (coin['mint'],ts,*values,now))


def change(a,b):
    return (b/a-1)*100 if a is not None and a>0 and b is not None else None


def evaluate(raw, snapshots, now, created=None):
    bars=valid_bars(raw,now)
    items=[]
    def add(key,label,ok,value,rule,reason='',source='chart'):
        items.append(dict(key=key,label=label,ok=ok,value=value,rule=rule,
                          reason=reason,source=source))
    def window(n):
        rows=bars[-n:]
        if len(rows)<n:return None,f'需要 {n} 根完整分钟线，已有 {len(rows)} 根'
        if now-rows[-1]['time']-60>180:return None,'最新收线超过 3 分钟'
        if any(b['time']-a['time']!=60 for a,b in zip(rows,rows[1:])):
            return None,f'最近 {n} 分钟存在缺口，不补零'
        return rows,''
    def avg(rows):return statistics.mean(b['volume'] for b in rows)

    w,reason=window(40)
    persistent=None
    if w:
        base=statistics.median(b['volume'] for b in w[:-10])
        ratios=[avg(w[-10:-5])/base,avg(w[-5:])/base] if base>0 else None
        share=max(b['volume'] for b in w[-10:])/sum(b['volume'] for b in w[-10:]) if sum(b['volume'] for b in w[-10:])>0 else 1
        persistent=bool(min(ratios)>=1.5 and share<=.4) if ratios else None
        value=f'前/后 5 分钟 {ratios[0]:.2f} / {ratios[1]:.2f} 倍；单分钟占比 {share:.1%}' if ratios else '历史成交量基准为零'
        reason='' if ratios else '无法定义放量倍数'
    else:value='—'
    add('sustained','持续放量',persistent,value,'最近两个 5 分钟均量均 ≥ 前 30 分钟中位量的 1.5 倍；单分钟占最近 10 分钟量 ≤ 40%',reason)

    w,reason=window(15)
    structure=None
    if w:
        groups=[w[i:i+5] for i in range(0,15,5)]
        highs=[max(b['high'] for b in g) for g in groups]
        lows=[min(b['low'] for b in g) for g in groups]
        structure=all(highs[i]>highs[i-1]*1.001 and lows[i]>lows[i-1]*1.001 for i in (1,2))
        value='高点 '+ ' → '.join(f'{x:.2f}' for x in highs)+'；低点 '+' → '.join(f'{x:.2f}' for x in lows)
    else:value='—'
    add('structure','阶梯式抬升',structure,value,'最近三个不重叠 5 分钟窗口，高点和低点逐段抬高 > 0.1%',reason)

    acceleration=None
    if w:
        gains=[change(g[0]['open'],g[-1]['close']) for g in groups]
        volumes=[avg(g) for g in groups]
        acceleration=0<gains[0]<gains[1]<gains[2] and 0<volumes[0]<volumes[1]<volumes[2]
        value='涨幅 '+ ' / '.join(f'{x:.2f}%' for x in gains)+'；均量 '+' / '.join(f'{x:.2f}' for x in volumes)
    add('acceleration','量价同步加速',acceleration,value,'三个 5 分钟窗口涨幅为正且递增，平均成交量同步递增',reason)

    w,reason=window(45)
    retest=None;value='—'
    if w:
        level=max(b['high'] for b in w[:30]);tail=w[30:]
        breakout=next((i for i,b in enumerate(tail) if b['close']>level*1.002),None)
        if breakout is None:retest=False;value='尚未突破前 30 分钟高点'
        else:
            after=tail[breakout+1:]
            touch=next((i for i,b in enumerate(after) if b['low']<=level*1.01),None)
            if not after or touch is None:
                value=f'突破位 {level:.2f}；等待回踩';reason='尚无可确认的回踩'
            else:
                retest=all(b['low']>=level*.98 for b in after) and touch<len(after)-1 and after[-1]['close']>max(level*1.002,after[touch]['high'])
                value=f'突破位 {level:.2f}；后续最低 {min(b["low"] for b in after):.2f}'
    add('retest','突破回踩再上行',retest,value,'前 30 分钟高点突破 0.2%；回踩至其 1% 内、低点不破 -2%，后续收盘再越回踩 K 线高点',reason)

    w,reason=window(30)
    coupling=None;value='—'
    if w:
        # Compare the latest adjacent up/down legs, not unrelated green/red candles.
        legs=[]
        for b in w:
            direction=1 if b['close']>b['open'] else -1 if b['close']<b['open'] else 0
            if not legs or legs[-1][0]!=direction:legs.append((direction,[b]))
            else:legs[-1][1].append(b)
        pairs=[(a,b) for a,b in zip(legs,legs[1:]) if a[0]==1 and b[0]==-1 and len(a[1])>=2 and len(b[1])>=2]
        if pairs:
            up,down=pairs[-1][0][1],pairs[-1][1][1]
            baseline=[b for b in w if b['time']<up[0]['time']]
            base=avg(baseline) if len(baseline)>=5 else 0
            if base>0 and avg(up)>0:
                ratio=avg(down)/avg(up)
                coupling=avg(up)>=base*1.5 and ratio<=.7 and down[-1]['close']>up[0]['open']
                value=f'上涨均量/基准 {avg(up)/base:.2f} 倍；回调/上涨均量 {ratio:.2f}'
            else:reason='上涨段之前至少需要 5 根非零基准线'
        else:reason='尚无相邻的至少两根上涨段及两根回调段'
    add('coupling','上涨放量 / 回调缩量',coupling,value,'最近相邻上涨/回调段：上涨均量 ≥ 基准 1.5 倍，回调均量 ≤ 上涨段 70%，未回吐整个涨幅',reason)

    multi=None;value='—'
    if w:
        detail=[];conditions=[]
        for size in (5,15):
            prev=w[-2*size:-size];cur=w[-size:]
            g=change(cur[0]['open'],cur[-1]['close']);v=avg(cur)/avg(prev) if avg(prev)>0 else None
            detail.append(f'{size} 分钟 {g:.2f}% / '+(f'{v:.2f} 倍' if v is not None else '基准为零'))
            conditions.append(g>0 and v>=1.2 if v is not None else None)
        multi=all(conditions) if all(x is not None for x in conditions) else None
        value='；'.join(detail)
    add('multi','5 / 15 分钟共振',multi,value,'两个窗口均上涨，成交量均 ≥ 各自前一等长窗口的 1.2 倍；窗口相关，不是独立证据',reason if multi is not None or not w else '成交量基准为零')

    w,reason=window(1450)
    revival=None;value='—'
    if created is None or now-created<=172800:
        reason='只适用于币龄超过 48 小时的代币'
    elif w:
        base=w[:-10];recent=w[-10:];med=statistics.median(b['volume'] for b in base)
        spread=(max(b['high'] for b in base)/min(b['low'] for b in base)-1)*100
        ratio=avg(recent)/med if med>0 else None
        revival=spread<=10 and ratio>=3 and recent[-1]['close']>max(b['high'] for b in base) if ratio is not None else None
        value=f'前 24 小时振幅 {spread:.2f}%；最近 10 分钟量比 '+(f'{ratio:.2f}' if ratio is not None else '未知')
        reason='' if ratio is not None else '24 小时成交量中位基准为零'
    add('revival','老币沉寂后复苏',revival,value,'币龄 > 48 小时；前 24 小时连续线振幅 ≤ 10%，最近 10 分钟量比 ≥ 3 且突破此前高点',reason)

    snaps=sorted([dict(s) for s in snapshots if number(s['ts']) is not None and s['ts']<=now],key=lambda s:s['ts'])
    latest=snaps[-1] if snaps else None
    fresh=latest is not None and now-latest['ts']<=600
    rates=[];growth=None;value='—';reason='需要三个新鲜来源快照，间隔各 4～7 分钟'
    if fresh:
        def previous(t):
            candidates=[s for s in snaps if 240<=t-s['ts']<=420]
            return min(candidates,key=lambda s:abs(t-s['ts']-300)) if candidates else None
        mid=previous(latest['ts']);first=previous(mid['ts']) if mid else None
        if first and all(s.get('holders') is not None and s['holders']>0 for s in (first,mid,latest)):
            rates=[change(a['holders'],b['holders'])/((b['ts']-a['ts'])/60) for a,b in ((first,mid),(mid,latest))]
            growth=rates[1]>0 and rates[1]>rates[0]
            value=f'{rates[0]:.3f} → {rates[1]:.3f}% / 分钟';reason=''
    add('holderAcceleration','持有人增长加速',growth,value,'两个相邻来源窗口的每分钟持有人增长率，后段为正且高于前段',reason,'snapshot')
    ratio=None
    if fresh and latest.get('marketcap') and latest.get('liquidity') is not None:
        ratio=100*latest['liquidity']/latest['marketcap']
    old=next((s for s in reversed(snaps) if latest and 240<=latest['ts']-s['ts']<=900 and s.get('marketcap') and s.get('liquidity') is not None),None)
    delta=ratio-100*old['liquidity']/old['marketcap'] if ratio is not None and old else None
    value=f'{ratio:.2f}%'+(f'；较基准 {delta:+.2f} 个百分点' if delta is not None else '；变化基准待积累') if ratio is not None else '—'
    add('depthRatio','流动性 / 市值',ratio>=5 if ratio is not None else None,value,'同一 OTC 快照相除，≥ 5% 为辅助门槛；不等于可成交深度或滑点', '' if ratio is not None else '缺少新鲜同源市值或流动性','snapshot')
    return dict(version=VERSION,items=items,coverage=dict(bars=len(bars),first=bars[0]['time'] if bars else None,last=bars[-1]['time']+60 if bars else None),
                known=sum(i['ok'] is not None for i in items),total=len(items))


def enrich(c, mint, evidence, coin, now):
    raw=[dict(r) for r in c.execute('SELECT * FROM otc_bars WHERE mint=? AND time>=? AND time<=? ORDER BY time',(mint,now-2*86400,now-60))]
    snapshots=[dict(r) for r in c.execute('SELECT * FROM otc_snapshots WHERE mint=? AND ts>=? AND ts<=? ORDER BY ts',(mint,now-1800,now))]
    result=evaluate(raw,snapshots,now,number(coin.get('createdAt')))
    evidence['historyTrend']=result
    for item in result['items']:
        if evidence['phase'] in ('unavailable','delayed'):
            item.update(ok=None,reason='当前来源异常或延迟，历史结果暂不用于当前判断')
    result['known']=sum(i['ok'] is not None for i in result['items'])
    flags={i['key']:i['ok'] for i in result['items']}
    if evidence['phase'] in ('risk','inactive','unavailable','delayed','warming'):
        return
    # Missing longer history preserves the weak early alert, never upgrades it.
    if evidence['phase'] in ('early','revival'):
        if flags['sustained'] is None:
            evidence['phase']='preview'
            evidence['quality']='基础异动；持续性历史待积累'
        elif flags['sustained'] is False:
            evidence['phase']='watch'
            evidence['reasons'].insert(0,'基础异动未通过持续放量检查')
        elif not any(flags[k] is True for k in ('structure','retest','multi')):
            evidence['phase']='watch'
            evidence['reasons'].insert(0,'持续放量成立，走势结构尚未配合')
    if flags['sustained'] is True and any(flags[k] is True for k in ('structure','retest','multi')):
        m=evidence['metrics']
        if m.get('liquidity',0)>=5000 and m.get('liquidityChange') is not None and m['liquidityChange']>=-5 and m.get('gain5m') is not None and 0<m['gain5m']<=15:
            evidence['phase']='revival' if flags['revival'] else 'early'
            evidence['quality']='持续放量与走势结构满足规则'
            evidence['reasons'].insert(0,'持续放量与走势结构同步成立；持有人增长仅作辅助')


def path_outcomes(c, now):
    """Observed full-minute paths, not execution returns or a hindsight backtest."""
    rows=[]
    events=c.execute("SELECT id,mint,opened,evidence FROM radar_events WHERE pair LIKE 'otc:%' ORDER BY opened DESC LIMIT 200").fetchall()
    import json
    for event in events:
        version=json.loads(event['evidence']).get('version','unknown')
        start=math.ceil(event['opened']/60)*60
        for minutes in (5,15,30):
            end=start+minutes*60
            row=dict(event=event['id'],version=version,minutes=minutes,status='pending' if now<end else 'missing')
            if now>=end:
                bars=c.execute('SELECT time,open,high,low,close FROM otc_bars WHERE mint=? AND time>=? AND time<? ORDER BY time',(event['mint'],start,end)).fetchall()
                if len(bars)==minutes and all(b['time']==start+i*60 for i,b in enumerate(bars)):
                    initial=bars[0]['open'];peak=initial;drawdown=0
                    for b in bars:
                        # Close-to-close drawdown avoids inventing intrabar high/low order.
                        peak=max(peak,b['close']);drawdown=min(drawdown,change(peak,b['close']))
                    row.update(status='observed',change=change(initial,bars[-1]['close']),
                        maximum=change(initial,max(b['high'] for b in bars)),
                        minimum=change(initial,min(b['low'] for b in bars)),drawdown=drawdown)
            rows.append(row)
    groups=[]
    for version in sorted({r['version'] for r in rows}):
        for minutes in (5,15,30):
            group=[r for r in rows if r['version']==version and r['minutes']==minutes]
            actual=[r for r in group if r['status']=='observed']
            groups.append(dict(version=version,minutes=minutes,total=len(group),observed=len(actual),
                missing=sum(r['status']=='missing' for r in group),pending=sum(r['status']=='pending' for r in group),
                positive=sum(r['change']>0 for r in actual)/len(actual) if actual else None,
                nonpositive=sum(r['change']<=0 for r in actual),
                **{k:statistics.median(r[k] for r in actual) if actual else None for k in ('change','maximum','minimum','drawdown')}))
    return dict(groups=groups,limit=200)
