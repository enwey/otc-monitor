import copy
import json
import unittest
from unittest.mock import patch

import monitor
import radar

NOW = 1800000000


def bars():
    rows = []
    for i in range(40):
        price = 100 if i < 37 else 100+(i-36)*.7
        rows.append(dict(time=NOW-(40-i)*60, open=price-.2 if i >= 37 else price,
                         close=price, high=price+.1, low=price-.3, volume=100 if i < 37 else 400))
    return rows


def pair():
    return dict(chainId='solana', baseToken={'address':'x'}, pairAddress='pool', priceUsd='1.02',
                liquidity={'usd':10000}, txns={'m5':{'buys':20,'sells':5}},
                volume={'m5':1500,'h1':6000}, priceChange={'m5':3})


class RadarTests(unittest.TestCase):
    def setUp(self):
        with patch.object(monitor, 'DB', ':memory:'):
            self.c = monitor.connect()
        radar.setup(self.c)
        self.addCleanup(self.c.close)

    def analyze(self, p=None, raw=None, created=None, now=NOW):
        return radar.analyze(p or pair(), bars() if raw is None else raw, created or NOW-3600, now, NOW)

    def test_early_evidence(self):
        e = self.analyze()
        self.assertEqual(e['phase'], 'early')
        self.assertAlmostEqual(e['metrics']['volumeAcceleration'], 1500/((6000-1500)/11))

    def test_zero_activity_is_not_missing_data(self):
        p=pair();p.pop('liquidity');p['dexId']='pumpfun'
        p['volume']['m5']=0;p['txns']['m5']={'buys':0,'sells':0};p['priceChange']={}
        e=self.analyze(p)
        self.assertEqual(e['phase'],'inactive')
        self.assertIsNone(e['metrics']['gain5m'])
        self.assertIsNone(e['metrics']['buyShare'])

    def test_two_sells_are_low_activity(self):
        p=pair();p.pop('liquidity');p['dexId']='pumpfun'
        p['volume']['m5']=12.57;p['txns']['m5']={'buys':0,'sells':2}
        self.assertEqual(self.analyze(p)['phase'],'inactive')

    def test_curve_signal_cannot_confirm(self):
        p=pair();p.pop('liquidity');p['dexId']='pumpfun'
        e=self.analyze(p);self.assertEqual(e['phase'],'curve')
        radar.record(self.c,'x',e,NOW)
        p['txns']['m5']['buys']+=1
        e=self.analyze(p);e['metrics']['chartAt']+=60
        radar.record(self.c,'x',e,NOW+60)
        self.assertEqual(e['phase'],'curve')

    def test_reference_depth_never_fills_verified_depth(self):
        p=pair();p.pop('liquidity')
        e=radar.analyze(p,bars(),NOW-3600,NOW,NOW,reference={'ts':NOW,'liquidity':50000})
        self.assertEqual(e['phase'],'unavailable')
        self.assertIsNone(e['metrics']['liquidity'])
        self.assertEqual(e['metrics']['referenceLiquidity'],50000)
        e=radar.analyze(p,bars(),NOW-3600,NOW,NOW,reference={'ts':NOW-301,'liquidity':50000})
        self.assertNotIn('referenceLiquidity',e['metrics'])

    def test_old_revival(self):
        self.assertEqual(self.analyze(created=NOW-86400*4)['phase'], 'revival')

    def test_missing_tx_is_unknown(self):
        p=pair(); p['txns']={}
        e=self.analyze(p)
        self.assertEqual(e['phase'], 'unavailable')
        self.assertIsNone(e['metrics']['buys'])

    def test_overheated_excluded(self):
        p=pair(); p['priceChange']['m5']=40
        self.assertEqual(self.analyze(p)['phase'], 'risk')

    def test_zero_baseline_not_infinite(self):
        raw=bars()
        for r in raw[:-3]: r['volume']=0
        self.assertIsNone(radar.candle_metrics(raw,NOW)['ratio'])

    def test_unclosed_bar_ignored(self):
        raw=bars(); extra=copy.deepcopy(raw[-1]);extra.update(time=NOW, close=1000, high=1000, volume=999999)
        self.assertEqual(radar.candle_metrics(raw,NOW),radar.candle_metrics(raw+[extra],NOW))

    def test_missing_minutes_not_filled(self):
        raw=bars();del raw[-5]
        self.assertEqual(self.analyze(raw=raw)['phase'],'warming')

    def test_stale_chart(self):
        raw=bars()
        for r in raw:r['time']-=600
        self.assertEqual(self.analyze(raw=raw)['phase'],'warming')

    def test_distinct_closed_bar_required(self):
        e=self.analyze();radar.record(self.c,'x',e,NOW)
        e=self.analyze();radar.record(self.c,'x',e,NOW+30)
        self.assertEqual(e['phase'],'early')
        p=pair();p['txns']['m5']['buys']+=1
        e=self.analyze(p);e['metrics']['chartAt']+=60
        radar.record(self.c,'x',e,NOW+60)
        self.assertEqual(e['phase'],'confirmed')
        self.assertEqual(self.c.execute('SELECT count(*) FROM radar_events').fetchone()[0],1)

    def test_unchanged_dex_cannot_confirm(self):
        radar.record(self.c,'x',self.analyze(),NOW)
        e=self.analyze();e['metrics']['chartAt']+=60
        radar.record(self.c,'x',e,NOW+60)
        self.assertEqual(e['phase'],'early')

    def test_pool_switch_resets_confirmation(self):
        radar.record(self.c,'x',self.analyze(),NOW)
        e=self.analyze();e['metrics']['pair']='different';e['metrics']['chartAt']+=60
        radar.record(self.c,'x',e,NOW+60)
        self.assertEqual(e['phase'],'early')
        self.assertEqual(self.c.execute('SELECT count(*) FROM radar_events WHERE closed IS NULL').fetchone()[0],1)

    def test_deepest_pool_no_double_count(self):
        p=pair();shallow=copy.deepcopy(p);shallow['liquidity']['usd']=100
        shallow['pairAddress']='shallow'
        self.assertEqual(radar.choose_pairs([shallow,p],{'x'})['x']['pairAddress'],'pool')
        self.assertEqual(radar.choose_pairs([p],{'not-x'}),{})

    def test_data_failure_not_market_failure(self):
        radar.record(self.c,'x',self.analyze(),NOW)
        e=self.analyze(raw=[]);radar.record(self.c,'x',e,NOW+60)
        self.assertEqual(self.c.execute('SELECT phase FROM radar_events').fetchone()[0],'delayed')

    def test_liquidity_exit(self):
        p=pair();p['liquidity']['usd']=8000
        history=[dict(pair='pool',ts=NOW-60,payload=json.dumps(pair()))]
        e=radar.analyze(p,bars(),NOW-1000,NOW,NOW,history)
        self.assertEqual(e['phase'],'risk')

    def test_stale_received_data(self):
        self.assertEqual(self.analyze(now=NOW+121)['phase'],'delayed')

    def test_full_cycle_bounded_and_failure_closes_as_delayed(self):
        class FakeSources:
            count=0
            broken=False
            def coins(s,page=0,sort='new'):
                s.count+=1
                return {'total':40000,'coins':[dict(mint='x',name='X',symbol='X',createdAt=NOW-3600,
                    snapshot=dict(at=NOW,usdPrice=1,liquidity=10000,holders=25))] if page==0 else []}
            def pairs(s,mints):
                s.count+=1
                if s.broken:raise ValueError('upstream unavailable')
                return {'x':pair()}
            def chart(s,mint):
                s.count+=1
                return bars()
        source=FakeSources()
        with patch('radar.time.time',return_value=NOW):
            radar.run(self.c,monitor.ingest,source)
        self.assertLessEqual(source.count,6)
        self.assertEqual(self.c.execute('SELECT phase FROM radar_events WHERE closed IS NULL').fetchone()[0],'early')
        source.broken=True;source.count=0
        with patch('radar.time.time',return_value=NOW+30):
            radar.run(self.c,monitor.ingest,source)
        self.assertEqual(source.count,1)
        self.assertEqual(self.c.execute('SELECT phase FROM radar_events').fetchone()[0],'delayed')

    def test_seed_cooldown_not_reset_by_discovery(self):
        coin=dict(mint='x',snapshot=dict(at=NOW,liquidity=10000,holders=25))
        radar.seed(self.c,coin,NOW,'test')
        self.c.execute('UPDATE radar_pool SET expires=?,cooldown=?',(NOW-1,NOW+3600))
        radar.seed(self.c,coin,NOW,'rediscovered')
        self.assertLess(self.c.execute('SELECT expires FROM radar_pool').fetchone()[0],NOW)

    def test_chart_failure_backoff_survives_dex_refresh(self):
        class Source:
            count=0
            charts=0
            def coins(s,page=0,sort='new'):
                s.count+=1
                return {'coins':[],'total':1}
            def pairs(s,mints):
                s.count+=1
                return {'x':pair()}
            def chart(s,mint):
                s.count+=1;s.charts+=1
                raise ValueError('502')
        source=Source()
        self.c.execute('INSERT INTO coins VALUES (?,?,?,?,?)',('x','X','X',NOW-3600,NOW))
        radar.seed(self.c,dict(mint='x',snapshot=dict(at=NOW,liquidity=10000,holders=25)),NOW,'test')
        with patch('radar.time.time',return_value=NOW):
            radar.run(self.c,monitor.ingest,source)
        with patch('radar.time.time',return_value=NOW+60):
            radar.run(self.c,monitor.ingest,source)
        self.assertEqual(source.charts,1)
        self.assertIn('K 线',self.c.execute('SELECT error FROM radar_pool').fetchone()[0])


if __name__=='__main__':
    unittest.main()
