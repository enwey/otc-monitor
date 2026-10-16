import json
import unittest
from unittest.mock import patch

import monitor
import otc_radar as otc
from test_radar import bars, NOW


def coin():
    return dict(mint='x',name='Example',symbol='EX',createdAt=NOW-3600,
                snapshot=dict(at=NOW,usdPrice=1.02,liquidity=10000,holders=30,volume24h=10000))


class OTCTests(unittest.TestCase):
    def setUp(self):
        with patch.object(monitor,'DB',':memory:'):
            self.c=monitor.connect()
        otc.setup(self.c)
        self.addCleanup(self.c.close)

    def evidence(self,c=None,raw=None):
        return otc.analyze(c or coin(),bars() if raw is None else raw,NOW,NOW,
                           [dict(ts=NOW-300,holders=25,liquidity=10000)])

    def test_native_positive(self):
        e=self.evidence()
        self.assertEqual(e['phase'],'early')
        self.assertEqual(e['metrics']['holdersAdded'],5)
        self.assertNotIn('buyShare',e['metrics'])
        self.assertNotIn('dexSignature',e['metrics'])
        self.assertEqual(e['score'],sum(p['points'] for p in e['scoreParts'] if p['ok']))
        self.assertEqual(len(e['scoreParts']),5)

    def test_no_history_no_signal(self):
        e=otc.analyze(coin(),bars(),NOW,NOW)
        self.assertEqual(e['phase'],'watch')
        self.assertIn('持有人增长',e['missing'])

    def test_stale_source_not_fresh_receive(self):
        p=coin();p['snapshot']['at']=NOW-601
        self.assertEqual(self.evidence(p)['phase'],'delayed')

    def test_gap_not_invented(self):
        raw=bars();del raw[-3]
        e=self.evidence(raw=raw)
        self.assertEqual(e['phase'],'warming')
        self.assertIsNone(e['metrics']['volume5m'])
        parts={p['label']:p for p in e['scoreParts']}
        self.assertIsNone(parts['相对放量']['ok'])
        self.assertTrue(parts['持有人增长']['ok'])
        self.assertEqual(e['partialScore'],40)
        self.assertEqual(e['score'],0)

    def test_stale_chart_preserves_snapshot_evidence(self):
        raw=bars()
        for b in raw:b['time']-=600
        e=self.evidence(raw=raw)
        self.assertEqual(e['phase'],'warming')
        self.assertIn('滞后',e['reasons'][0])
        self.assertEqual(e['partialScore'],40)
        self.assertIsNone(e['scoreParts'][1]['ok'])

    def test_chart_failure_does_not_hide_snapshot_checks(self):
        self.c.execute('INSERT INTO samples VALUES (?,?,?,?,?,?)',('x',NOW-300,1,10000,25,9000))
        row=dict(mint='x',payload=json.dumps(coin()),candles=json.dumps(bars()),checked=NOW,error='K 线 OTC：HTTP 502')
        e=otc.evidence(self.c,row,NOW)
        self.assertEqual(e['phase'],'unavailable')
        self.assertEqual(e['score'],0)
        self.assertEqual(e['partialScore'],40)
        self.assertIsNone(e['scoreParts'][0]['ok'])
        self.assertTrue(e['scoreParts'][2]['ok'])

    def test_no_snapshot(self):
        p=coin();p['snapshot']={}
        self.assertEqual(self.evidence(p)['phase'],'unavailable')

    def test_no_holder_growth(self):
        p=coin();p['snapshot']['holders']=25
        self.assertEqual(self.evidence(p)['phase'],'watch')

    def test_endpoint_allowlist(self):
        with self.assertRaises(ValueError):otc.Sources().get('https://other.example/api')

    def test_collector_and_report(self):
        class Fake:
            count=0
            cache={}
            def coins(s,page=0,sort='new',mint=None):
                s.count+=1;s.cache['x']=coin()
                return {'coins':[coin()],'total':1}
            def chart(s,mint):s.count+=1;return bars()
        with patch('time.time',return_value=NOW):
            self.c.execute('INSERT INTO samples VALUES (?,?,?,?,?,?)',('x',NOW-300,1,10000,25,9000))
            otc.run(self.c,monitor.ingest,Fake())
            result=otc.report(self.c,NOW)
        # A three-minute spike does not satisfy two sustained five-minute windows.
        self.assertEqual(len(result['live']),0)
        self.assertEqual(result['watch'][0]['evidence']['version'],'otc-2.0+history-1.0')
        self.assertEqual(result['watch'][0]['evidence']['historyTrend']['items'][0]['ok'],False)
        self.assertEqual(result['capabilities']['source'],'OTC only')

    def test_migration_closes_legacy_without_deleting(self):
        e=self.evidence();e['version']='radar-1.2'
        self.c.execute("INSERT INTO radar_events(mint,opened,updated,phase,evidence) VALUES (?,?,?,?,?)",('x',NOW,NOW,'early',json.dumps(e)))
        self.c.execute("DELETE FROM state WHERE key='source_version'")
        otc.setup(self.c)
        r=self.c.execute('SELECT * FROM radar_events').fetchone()
        self.assertEqual(r['phase'],'rule_changed')
        self.assertIsNotNone(r['closed'])

    def test_followup_uses_otc_source_time(self):
        class Fake:
            count=0
            def __init__(s):s.cache={}
            def coins(s,page=0,sort='new',mint=None):
                p=coin();p['snapshot']['at']=NOW+300;p['snapshot']['usdPrice']=1.122
                s.cache['x']=p;s.count+=1
                return dict(coins=[p],total=1)
            def chart(s,mint):s.count+=1;return bars()
        self.c.execute('INSERT INTO coins VALUES (?,?,?,?,?)',('x','Example','EX',NOW-3600,NOW))
        otc.legacy.record(self.c,'x',self.evidence(),NOW)
        with patch('time.time',return_value=NOW+300):otc.run(self.c,monitor.ingest,Fake())
        result=self.c.execute('SELECT * FROM event_outcomes').fetchone()
        self.assertIsNotNone(result)
        self.assertEqual(result['observed'],NOW+300)
        self.assertAlmostEqual(result['change'],10)


if __name__=='__main__':unittest.main()
