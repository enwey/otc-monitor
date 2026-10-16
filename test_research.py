import json
import unittest
from unittest.mock import patch
import monitor
import radar
import research
from test_radar import NOW, bars, pair


class ResearchTests(unittest.TestCase):
    def setUp(self):
        with patch.object(monitor,'DB',':memory:'):
            self.c=monitor.connect()
        radar.setup(self.c)
        self.addCleanup(self.c.close)
        self.c.execute('INSERT INTO coins VALUES (?,?,?,?,?)',('x','X','X',NOW-3600,NOW))
        radar.seed(self.c,dict(mint='x',snapshot=dict(at=NOW,liquidity=10000,holders=25)),NOW,'test')

    def event(self):
        e=radar.analyze(pair(),bars(),NOW-3600,NOW,NOW)
        radar.record(self.c,'x',e,NOW)
        return self.c.execute('SELECT * FROM radar_events').fetchone()

    def test_favorite_persists_and_removes(self):
        research.favorite(self.c,'x',True,NOW)
        research.favorite(self.c,'x',True,NOW)
        self.assertEqual(self.c.execute('SELECT count(*) FROM favorites').fetchone()[0],1)
        research.favorite(self.c,'x',False,NOW)
        self.assertEqual(self.c.execute('SELECT count(*) FROM favorites').fetchone()[0],0)

    def test_unknown_favorite_rejected(self):
        with self.assertRaises(ValueError):research.favorite(self.c,'unknown',True,NOW)

    def test_immutable_frames(self):
        event=self.event();first=self.c.execute('SELECT evidence FROM event_frames').fetchone()[0]
        p=pair();p['priceUsd']='1.2';p['txns']['m5']['buys']=21
        e=radar.analyze(p,bars(),NOW-3600,NOW+60,NOW+60)
        e['metrics']['chartAt']+=60
        radar.record(self.c,'x',e,NOW+60)
        replay=research.replay(self.c,event['id'],NOW+60)
        self.assertEqual(len(replay['frames']),2)
        self.assertEqual(self.c.execute('SELECT evidence FROM event_frames ORDER BY id LIMIT 1').fetchone()[0],first)

    def test_outcomes_no_lookahead_or_late_fill(self):
        e=self.event()
        research.measure(self.c,e,pair(),NOW+299)
        self.assertEqual(self.c.execute('SELECT count(*) FROM event_outcomes').fetchone()[0],0)
        research.measure(self.c,e,pair(),NOW+500)
        self.assertEqual(self.c.execute('SELECT count(*) FROM event_outcomes').fetchone()[0],0)
        research.measure(self.c,e,pair(),NOW+301)
        rows=research.outcome_rows(self.c,e,NOW+500)
        self.assertEqual(rows[0]['status'],'observed')
        self.assertAlmostEqual(rows[0]['change'],0)

    def test_closed_signals_still_followed(self):
        e=self.event();self.c.execute("UPDATE radar_events SET closed=?,phase='faded'",(NOW+60,))
        self.assertEqual(len(research.targets(self.c,NOW+300)),1)
        q=research.quality(self.c,NOW+2000)
        self.assertEqual(q['groups'][0]['horizons'][0]['missing'],1)
        self.assertIsNone(q['groups'][0]['horizons'][0]['positive'])

    def test_pool_switch_not_compared(self):
        e=self.event();p=pair();p['pairAddress']='another'
        research.measure(self.c,e,p,NOW+300)
        self.assertEqual(self.c.execute('SELECT count(*) FROM event_outcomes').fetchone()[0],0)

    def test_weak_new_coin_not_confirmed(self):
        raw=bars()[-6:]
        for t in [NOW,NOW+60,NOW+120]:
            p=pair();p['txns']['m5']['buys']+=int((t-NOW)/60)
            e=radar.analyze(p,raw,NOW-400,t,t)
            self.assertEqual(e['phase'],'preview')
            radar.record(self.c,'x',e,t)
            self.assertEqual(e['phase'],'preview')

    def test_sparse_cohort_uses_floor(self):
        self.c.execute('UPDATE radar_pool SET checked=?,payload=?',(NOW,json.dumps(pair())))
        context=research.cohort_context(self.c,NOW)['x']
        self.assertFalse(context['applied'])
        self.assertEqual(context['minimumVolume'],300)

    def test_first_hour_does_not_assume_55_minute_baseline(self):
        e=radar.analyze(pair(),bars()[-10:],NOW-600,NOW,NOW)
        self.assertAlmostEqual(e['metrics']['volumeAcceleration'],1500/4500)

    def test_followup_does_not_poll_between_horizons(self):
        self.event()
        self.assertEqual(len(research.targets(self.c,NOW+500)),0)
        self.assertEqual(len(research.targets(self.c,NOW+900)),1)

    def test_favorite_limit(self):
        for i in range(20):
            self.c.execute('INSERT INTO favorites VALUES (?,?)',(str(i),NOW))
        with self.assertRaises(ValueError):research.favorite(self.c,'x',True,NOW)

    def test_rule_change_starts_separate_event(self):
        event=self.event()
        e=json.loads(event['evidence']);e['version']='older'
        self.c.execute('UPDATE radar_events SET evidence=?',(json.dumps(e),))
        radar.record(self.c,'x',radar.analyze(pair(),bars(),NOW-3600,NOW,NOW),NOW+30)
        self.assertEqual(self.c.execute('SELECT count(*) FROM radar_events').fetchone()[0],2)

    def test_full_cohort_adjusts_threshold(self):
        for i in range(8):
            mint=str(i)
            self.c.execute('INSERT INTO coins VALUES (?,?,?,?,?)',(mint,mint,mint,NOW-3600,NOW))
            self.c.execute('INSERT INTO radar_pool(mint,expires,checked,payload) VALUES (?,?,?,?)',
                           (mint,NOW+1000,NOW,json.dumps(pair())))
        context=research.cohort_context(self.c,NOW)['0']
        self.assertTrue(context['applied'])
        self.assertEqual(context['minimumVolume'],750)


if __name__=='__main__':unittest.main()
