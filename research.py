"""Immutable evidence replay and forward observation, not a profit backtest."""
import json
import statistics

HORIZONS = (300, 900, 1800)
VERSION = 'radar-1.2'


def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS favorites(mint TEXT PRIMARY KEY, added REAL);
    CREATE TABLE IF NOT EXISTS event_frames(
      id INTEGER PRIMARY KEY, event_id INTEGER, ts REAL, phase TEXT, evidence TEXT);
    CREATE INDEX IF NOT EXISTS frames_event ON event_frames(event_id,ts);
    CREATE TABLE IF NOT EXISTS event_outcomes(
      event_id INTEGER, horizon INTEGER, observed REAL, price REAL, change REAL,
      status TEXT, PRIMARY KEY(event_id,horizon));
    ''')


def favorite(c, mint, enabled, now):
    if not isinstance(mint, str) or not c.execute('SELECT 1 FROM radar_pool WHERE mint=?', (mint,)).fetchone():
        raise ValueError('只能关注已发现的候选合约')
    if enabled:
        exists=c.execute('SELECT 1 FROM favorites WHERE mint=?',(mint,)).fetchone()
        if not exists and c.execute('SELECT count(*) FROM favorites').fetchone()[0] >= 20:
            raise ValueError('关注上限 20 个，请先移除其他标的')
        c.execute('INSERT OR IGNORE INTO favorites VALUES (?,?)', (mint,now))
        c.execute('UPDATE radar_pool SET expires=max(expires,?) WHERE mint=?', (now+900,mint))
    else:
        c.execute('DELETE FROM favorites WHERE mint=?', (mint,))
    c.commit()


def capture(c, mint, now):
    event = c.execute('SELECT * FROM radar_events WHERE mint=? ORDER BY id DESC LIMIT 1',(mint,)).fetchone()
    if not event or event['updated'] != now:
        return
    last = c.execute('SELECT * FROM event_frames WHERE event_id=? ORDER BY id DESC LIMIT 1',(event['id'],)).fetchone()
    ev=json.loads(event['evidence'])
    if last:
        old=json.loads(last['evidence'])
        changed=last['phase'] != event['phase'] or old['metrics'].get('chartAt') != ev['metrics'].get('chartAt')
        if not changed and now-last['ts'] < 60:
            return
    c.execute('INSERT INTO event_frames(event_id,ts,phase,evidence) VALUES (?,?,?,?)',
              (event['id'],now,event['phase'],event['evidence']))


def targets(c, now):
    # Continue observing closed signals too; do not select only surviving winners.
    events=c.execute('''SELECT e.* FROM radar_events e WHERE e.opened>=? AND e.opened<=?
        AND EXISTS (SELECT 1 FROM event_frames f WHERE f.event_id=e.id)
        AND (SELECT count(*) FROM event_outcomes o WHERE o.event_id=e.id)<3
        ORDER BY e.opened ASC''', (now-1920,now-300)).fetchall()
    return [e for e in events if any(e['opened']+h<=now<=e['opened']+h+120 and not c.execute(
        'SELECT 1 FROM event_outcomes WHERE event_id=? AND horizon=?',(e['id'],h)).fetchone() for h in HORIZONS)][:60]


def measure(c, event, pair, now):
    for horizon in HORIZONS:
        due=event['opened']+horizon
        if not due <= now <= due+120:
            continue
        if c.execute('SELECT 1 FROM event_outcomes WHERE event_id=? AND horizon=?',(event['id'],horizon)).fetchone():
            continue
        if not pair or pair.get('pairAddress') != event['pair']:
            continue
        try:
            price=float(pair['priceUsd'])
            if not 0 < price < float('inf') or not event['initial_price']:
                continue
        except (KeyError,ValueError,TypeError):
            continue
        change=(price/event['initial_price']-1)*100
        c.execute('INSERT INTO event_outcomes VALUES (?,?,?,?,?,?)',
                  (event['id'],horizon,now,price,change,'observed'))


def outcome_rows(c, event, now):
    stored={r['horizon']:dict(r) for r in c.execute('SELECT * FROM event_outcomes WHERE event_id=?',(event['id'],))}
    return [stored.get(h,dict(horizon=h,status='pending' if now<=event['opened']+h+120 else 'missing',
                             change=None,observed=None)) for h in HORIZONS]


def quality(c, now):
    events=c.execute('''SELECT e.* FROM radar_events e WHERE EXISTS
                       (SELECT 1 FROM event_frames f WHERE f.event_id=e.id) ORDER BY opened DESC''').fetchall()
    groups={}
    for event in events:
        first=c.execute('SELECT evidence,phase FROM event_frames WHERE event_id=? ORDER BY id LIMIT 1',(event['id'],)).fetchone()
        ev=json.loads(first['evidence']); key=(ev.get('version','unknown'),first['phase'])
        groups.setdefault(key,[]).append(event)
    result=[]
    for (version,phase), group in groups.items():
        horizons=[]
        for h in HORIZONS:
            rows=[next(x for x in outcome_rows(c,e,now) if x['horizon']==h) for e in group]
            actual=[x['change'] for x in rows if x['status']=='observed']
            horizons.append(dict(horizon=h,total=len(group),observed=len(actual),
                pending=sum(x['status']=='pending' for x in rows),missing=sum(x['status']=='missing' for x in rows),
                median=statistics.median(actual) if actual else None,
                positive=sum(x>0 for x in actual)/len(actual) if actual else None))
        result.append(dict(version=version,phase=phase,horizons=horizons))
    return dict(events=len(events),groups=result)


def replay(c, event_id, now):
    event=c.execute('SELECT * FROM radar_events WHERE id=?',(event_id,)).fetchone()
    if not event:
        return None
    frames=[dict(r) for r in c.execute('SELECT * FROM event_frames WHERE event_id=? ORDER BY id',(event_id,))]
    for frame in frames:frame['evidence']=json.loads(frame['evidence'])
    return dict(frames=frames,outcomes=outcome_rows(c,event,now))


def cohort_context(c, now):
    """Contemporaneous, local cohort only. Sparse groups explicitly fall back."""
    groups={}; members={}
    for row in c.execute('''SELECT p.mint,p.payload,c.created FROM radar_pool p JOIN coins c ON c.mint=p.mint
                            WHERE p.expires>? AND p.checked>? AND p.payload IS NOT NULL''',(now,now-120)):
        p=json.loads(row['payload']);depth=(p.get('liquidity') or {}).get('usd');v=(p.get('volume') or {}).get('m5')
        if not isinstance(depth,(int,float)) or depth<=0 or not isinstance(v,(int,float)) or v<0:
            continue
        age=now-row['created'] if row['created'] else None
        age_band='首小时' if age is not None and age<3600 else '新币' if age is not None and age<172800 else '老币'
        depth_band='低深度' if depth<20000 else '中深度' if depth<100000 else '高深度'
        key=age_band+' / '+depth_band
        groups.setdefault(key,[]).append(v);members[row['mint']]=(key,v)
    result={}
    for mint,(key,v) in members.items():
        values=groups[key];med=statistics.median(values)
        result[mint]=dict(label=key,size=len(values),medianVolume=med,
            percentile=round(100*sum(x<=v for x in values)/len(values)),
            minimumVolume=max(300,min(1500,med*.5)) if len(values)>=8 else 300,
            applied=len(values)>=8)
    return result
