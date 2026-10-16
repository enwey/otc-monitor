import sqlite3
import unittest
import trend_history as h

NOW=1800000000

def bars(n=60):
    return [dict(time=NOW-(n-i)*60,open=100,high=100.2,low=99.8,close=100,volume=100) for i in range(n)]

def evaluate(b,s=()):
    return {i['key']:i for i in h.evaluate(b,s,NOW,NOW-3*86400)['items']}

class TrendHistoryTests(unittest.TestCase):
    def test_flat_not_trending(self):
        r=evaluate(bars())
        for key in ('sustained','structure','acceleration','multi','retest'):
            self.assertFalse(r[key]['ok'],key)

    def test_sustained_and_spike(self):
        b=bars()
        for x in b[-10:]:x['volume']=200
        self.assertTrue(evaluate(b)['sustained']['ok'])
        b[-1]['volume']=10000
        self.assertFalse(evaluate(b)['sustained']['ok'])

    def test_gap_unknown(self):
        b=bars();del b[-4]
        r=evaluate(b)
        for key in ('sustained','structure','acceleration','multi','retest','coupling'):
            self.assertIsNone(r[key]['ok'],key)

    def test_stale_unknown(self):
        b=bars()
        for x in b:x['time']-=600
        self.assertIsNone(evaluate(b)['sustained']['ok'])

    def test_zero_baseline_unknown(self):
        b=bars()
        for x in b:x['volume']=0
        self.assertIsNone(evaluate(b)['sustained']['ok'])
        self.assertIsNone(evaluate(b)['multi']['ok'])

    def test_structure_acceleration_multiframe(self):
        b=bars();price=100
        for i,x in enumerate(b[-15:]):
            step=(i//5+1)*.2
            x.update(open=price,low=price-.05,close=price+step,high=price+step+.05,volume=200+100*(i//5))
            price+=step
        r=evaluate(b)
        for k in ('structure','acceleration','multi','sustained'):self.assertTrue(r[k]['ok'],k)

    def test_retest_and_failure(self):
        b=bars(45)
        for x in b[-15:]:x.update(open=101,close=101.1,high=101.2,low=100.5)
        b[-1].update(open=101.1,close=102,high=102.1,low=101)
        self.assertTrue(evaluate(b)['retest']['ok'])
        b[-4]['low']=96
        self.assertFalse(evaluate(b)['retest']['ok'])

    def test_up_and_down_legs(self):
        b=bars(30);price=100
        for x in b[-10:-5]:
            x.update(open=price,low=price-.1,close=price+1,high=price+1.1,volume=200);price+=1
        for x in b[-5:]:
            x.update(open=price,low=price-.3,close=price-.2,high=price+.1,volume=80);price-=.2
        self.assertTrue(evaluate(b)['coupling']['ok'])
        for x in b[-5:]:x['volume']=300
        self.assertFalse(evaluate(b)['coupling']['ok'])

    def test_holders_and_depth(self):
        s=[dict(ts=NOW-600,holders=100,liquidity=1000,marketcap=20000),dict(ts=NOW-300,holders=102,liquidity=1100,marketcap=20000),dict(ts=NOW,holders=110,liquidity=1200,marketcap=20000)]
        r=evaluate([],s)
        self.assertTrue(r['holderAcceleration']['ok'])
        self.assertTrue(r['depthRatio']['ok'])
        self.assertIn('6.00%',r['depthRatio']['value'])
        self.assertIsNone(evaluate([],[dict(s[0],ts=NOW-601)])['depthRatio']['ok'])

    def test_long_revival(self):
        b=bars(1450)
        for x in b[-10:]:x.update(open=101,close=102,low=100.5,high=102.1,volume=400)
        self.assertTrue(evaluate(b)['revival']['ok'])
        del b[20]
        self.assertIsNone(evaluate(b)['revival']['ok'])

    def test_archive_dedup_and_closed_only(self):
        c=sqlite3.connect(':memory:');h.setup(c)
        b=bars(3)+[dict(time=NOW,open=100,close=100,low=99,high=101,volume=1)]
        h.save_bars(c,'x',b,NOW);b[0]['volume']=999;h.save_bars(c,'x',b,NOW)
        self.assertEqual(c.execute('SELECT count(*) FROM otc_bars').fetchone()[0],3)
        self.assertEqual(c.execute('SELECT volume FROM otc_bars ORDER BY time LIMIT 1').fetchone()[0],100)
        coin=dict(mint='x',snapshot=dict(at=NOW,holders=10,liquidity=5000,marketCap=10000))
        h.save_snapshot(c,coin,NOW);h.save_snapshot(c,coin,NOW)
        self.assertEqual(c.execute('SELECT count(*) FROM otc_snapshots').fetchone()[0],1)
        c.close()

    def test_invalid_and_future_excluded(self):
        b=bars();b[-1]['volume']=float('nan');b[-2]['time']=NOW+60;b[-3]['low']=10000
        self.assertEqual(len(h.valid_bars(b,NOW)),57)

    def test_paths_require_every_bar(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;h.setup(c)
        c.execute('CREATE TABLE radar_events(id INTEGER,mint TEXT,opened REAL,evidence TEXT,pair TEXT)')
        c.execute('INSERT INTO radar_events VALUES (1,?,?,?,?)',('x',NOW-300,'{"version":"test"}','otc:x'))
        h.save_bars(c,'x',bars(5),NOW)
        groups=h.path_outcomes(c,NOW)['groups']
        self.assertEqual(groups[0]['observed'],1)
        self.assertEqual(groups[0]['change'],0)
        self.assertEqual(groups[0]['nonpositive'],1)
        self.assertEqual(groups[1]['pending'],1)
        c.execute('DELETE FROM otc_bars WHERE time=?',(NOW-120,))
        self.assertEqual(h.path_outcomes(c,NOW)['groups'][0]['missing'],1)
        c.close()

    def test_signal_without_holder_growth_and_risk_gate(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;h.setup(c)
        b=bars();price=100
        for i,x in enumerate(b[-15:]):
            step=.2*(i//5+1)
            x.update(open=price,low=price-.05,close=price+step,high=price+step+.05,volume=200+100*(i//5));price+=step
        h.save_bars(c,'x',b,NOW)
        for phase,expected in [('watch','early'),('risk','risk'),('unavailable','unavailable')]:
            e=dict(phase=phase,metrics=dict(liquidity=10000,liquidityChange=0,gain5m=2,holdersAdded=0),reasons=[])
            h.enrich(c,'x',e,dict(createdAt=NOW-3600),NOW)
            self.assertEqual(e['phase'],expected)
            if phase=='unavailable':self.assertTrue(all(i['ok'] is None for i in e['historyTrend']['items']))
        c.close()
