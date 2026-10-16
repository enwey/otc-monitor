"""Bounded discovery and adaptive watchlist; never sweeps the full registry."""
import json
import time

POOL_LIMIT = 36
DETAIL_BUDGET = 12
BOARD_INTERVAL = 300
BOARD_PAGES = 3
NEW_MAX_PAGES = 3
NEW_AGE = 48 * 3600
QUIET_TTL = 600
COOLDOWN = 3600

def setup(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS watchlist(
      mint TEXT PRIMARY KEY, kind TEXT, score REAL, reason TEXT,
      added REAL, motion REAL, checked REAL, status TEXT, cooldown REAL,
      sample_ts REAL, gain REAL, holder_gain REAL);
    CREATE TABLE IF NOT EXISTS leaderboard(mint TEXT PRIMARY KEY, rank INTEGER);
    ''')

def get_state(c, key, default=None):
    row = c.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default

def put(c,key,value):
    c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value)))

def viable(s, now, old=False):
    if not s or not s['ts'] or not (-60 <= now-s['ts'] <= 300):
        return False
    if not s['price'] or s['liquidity'] is None or s['holders'] is None:
        return False
    return s['liquidity'] >= (5000 if old else 2500) and s['holders'] >= (20 if old else 5)

def movement(rows):
    if not rows:
        return None
    last=rows[-1]
    bases=[r for r in rows if 120 <= last['ts']-r['ts'] <= 600 and r['price'] and r['holders'] is not None and r['liquidity']]
    if not bases or any(last[k] is None for k in ('price','holders','liquidity')):
        return None
    first=min(bases,key=lambda r:abs(last['ts']-r['ts']-300))
    return ((last['price']/first['price']-1)*100,
            last['holders']-first['holders'],last['liquidity']/first['liquidity'])

def latest(c,mint,now):
    return c.execute('SELECT * FROM samples WHERE mint=? AND ts>=? ORDER BY ts',(mint,now-900)).fetchall()

def retire(c,mint,now,reason):
    c.execute("UPDATE watchlist SET status='dormant',score=0,reason=?,cooldown=? WHERE mint=?",(reason,now+COOLDOWN,mint))

def evaluate(c,mint,now):
    w=c.execute('SELECT * FROM watchlist WHERE mint=?',(mint,)).fetchone()
    if not w or w['status']=='dormant':
        return
    rows=latest(c,mint,now)
    s=rows[-1] if rows else None
    if not viable(s,now,w['kind']=='revival'):
        retire(c,mint,now,'行情过期或流动性／持有人不足')
        return
    delta=movement(rows)
    if delta and (delta[0] < -8 or delta[0] > 50 or delta[2] < .9):
        retire(c,mint,now,'回落、流动性流失或涨幅过热')
        return
    changed=s['ts'] > (w['sample_ts'] or 0)
    growing=delta and 1 <= delta[0] <= 30 and delta[1] >= 1 and delta[2] >= .95
    motion=now if growing and changed else w['motion']
    if now-motion >= QUIET_TTL:
        retire(c,mint,now,'10 分钟未持续出现价格与持有人同步增长')
        return
    score=min(100,40+max(0,delta[0])*2+min(20,max(0,delta[1])*2)) if growing else max(1,25-(now-w['added'])/40)
    reason='价格与持有人同步增长' if growing else '观察中：等待增量资料确认'
    c.execute('''UPDATE watchlist SET score=?,reason=?,motion=?,sample_ts=?,gain=?,holder_gain=? WHERE mint=?''',
              (score,reason,motion,s['ts'],delta[0] if delta else None,delta[1] if delta else None,mint))

def admit(c,mint,kind,now,reason):
    rows=latest(c,mint,now)
    if not viable(rows[-1] if rows else None,now,kind=='revival'):
        return
    w=c.execute('SELECT * FROM watchlist WHERE mint=?',(mint,)).fetchone()
    if w and w['status']=='active':
        evaluate(c,mint,now)
        return
    # Rediscovery alone must not reset the quiet timer or defeat cooldown.
    if w and now < w['cooldown']:
        return
    delta=movement(rows)
    score=20
    if delta:
        if delta[0] < -8 or delta[0] > 50 or delta[2] < .9:
            return
        if 1 <= delta[0] <= 30 and delta[1]>=1:
            score=min(100,40+delta[0]*2+min(20,delta[1]*2))
    c.execute('INSERT OR REPLACE INTO watchlist VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
              (mint,kind,score,reason,now,now,now,'active',0,rows[-1]['ts'],delta[0] if delta else None,delta[1] if delta else None))

def trim(c,now):
    rows=c.execute("SELECT mint FROM watchlist WHERE status='active' ORDER BY score DESC,added DESC").fetchall()
    for row in rows[POOL_LIMIT:]:
        retire(c,row['mint'],now,'候选池容量限制，优先保留动能较强者')

def due(c,now,exclude):
    # Least recently checked first prevents a few popular tokens starving others.
    return [r['mint'] for r in c.execute("SELECT mint FROM watchlist WHERE status='active' AND checked<=? ORDER BY checked ASC,score DESC",(now-45,)) if r['mint'] not in exclude][:DETAIL_BUDGET]

def run_cycle(c,fetch,ingest):
    setup(c)
    started=time.time()
    requests=0
    seen=set()
    def read(page=0,query=None,sort='new'):
        nonlocal requests
        if requests:
            time.sleep(.5)
        requests+=1
        data=fetch(page,query,sort)
        if query:
            data=dict(data,coins=[coin for coin in data['coins'] if coin.get('mint')==query])
        ingest(c,data)
        now=time.time()
        for coin in data['coins']:
            mint=coin.get('mint')
            seen.add(mint)
            c.execute("UPDATE watchlist SET checked=? WHERE mint=? AND status='active'",(now,mint))
        return data

    watermark=get_state(c,'new_watermark',0)
    newest=watermark
    gap=False
    for page in range(NEW_MAX_PAGES):
        data=read(page)
        put(c,'total',data['total'])
        coins=data['coins']
        times=[coin.get('createdAt') or 0 for coin in coins]
        newest=max([newest]+times)
        now=time.time()
        for coin in coins:
            age=now-(coin.get('createdAt') or 0)
            if 0<=age<=NEW_AGE:
                admit(c,coin['mint'],'new',now,'48 小时内新币：等待动能确认')
        # Bootstrap reads only a bounded recent batch; later runs stop at the watermark.
        if not coins or len(coins)<60 or min(times)<=max(watermark,now-NEW_AGE):
            break
        gap=page==NEW_MAX_PAGES-1 and watermark>0
    put(c,'new_watermark',newest)
    put(c,'discoveryGap',gap)

    if started-get_state(c,'board_at',0)>=BOARD_INTERVAL:
        previous={r['mint']:r['rank'] for r in c.execute('SELECT * FROM leaderboard')}
        ranking={}
        entrants=0
        for page in range(BOARD_PAGES):
            board=read(page,sort='volume24h')
            for index,coin in enumerate(board['coins']):
                mint=coin['mint']; rank=page*60+index+1
                ranking[mint]=rank
                old_rank=previous.get(mint)
                # Entry or a significant rank jump is a discovery cue, not a trade signal.
                trigger=bool(previous) and (old_rank is None or old_rank-rank>=max(5,old_rank*.2))
                if trigger:
                    entrants+=1
                    kind='new' if 0<=time.time()-(coin.get('createdAt') or 0)<=NEW_AGE else 'revival'
                    admit(c,mint,kind,time.time(),'活跃榜新进／排名上升，待价格与持有人确认')
        c.execute('DELETE FROM leaderboard')
        c.executemany('INSERT INTO leaderboard VALUES (?,?)',ranking.items())
        put(c,'board_at',time.time())
        put(c,'boardEntrants',entrants)

    now=time.time()
    trim(c,now)
    selected=due(c,now,seen)
    for mint in selected:
        data=read(query=mint)
        if not data['coins']:
            retire(c,mint,time.time(),'接口未返回该合约')
        else:
            evaluate(c,mint,time.time())
    # Give scheduled candidates a refresh before judging cached data as stale.
    for row in c.execute("SELECT mint FROM watchlist WHERE status='active'").fetchall():
        evaluate(c,row['mint'],time.time())
    trim(c,time.time())
    put(c,'mode','adaptive')
    put(c,'requests',requests)
    put(c,'detailChecks',len(selected))
    put(c,'poolLimit',POOL_LIMIT)
    put(c,'lastSuccess',time.time())
    put(c,'error',None)
    put(c,'cycleSeconds',round(time.time()-started,1))
    c.execute("DELETE FROM state WHERE key IN ('cursor','pages')")
    c.execute('DELETE FROM samples WHERE ts<?',(started-7*86400,))
    c.commit()
