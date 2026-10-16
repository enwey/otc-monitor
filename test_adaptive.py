import unittest
from unittest.mock import patch

import adaptive as a
import monitor


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        with patch.object(monitor, 'DB', ':memory:'):
            self.c = monitor.connect()
        a.setup(self.c)
        self.addCleanup(self.c.close)

    def sample(self, mint='x', ts=10000, price=1, holders=25):
        self.c.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)',
                       (mint, ts, price, 6000, holders, 1000))

    def entry(self, mint='x', now=10000):
        self.sample(mint, now)
        a.admit(self.c, mint, 'new', now, 'test')

    def row(self, mint='x'):
        return self.c.execute('SELECT * FROM watchlist WHERE mint=?', (mint,)).fetchone()

    def test_cooldown(self):
        self.entry()
        a.retire(self.c, 'x', 10000, 'quiet')
        self.sample(ts=10100)
        a.admit(self.c, 'x', 'new', 10100, 'rediscovered')
        self.assertEqual(self.row()['status'], 'dormant')

    def test_cached_snapshot_does_not_extend_motion(self):
        self.entry(now=9700)
        self.sample(ts=10000, price=1.05, holders=28)
        a.evaluate(self.c, 'x', 10000)
        self.assertEqual(self.row()['motion'], 10000)
        a.evaluate(self.c, 'x', 10100)
        self.assertEqual(self.row()['motion'], 10000)

    def test_quiet_eviction_with_fresh_data(self):
        self.entry()
        self.sample(ts=10600)
        a.evaluate(self.c, 'x', 10600)
        self.assertEqual(self.row()['status'], 'dormant')

    def test_capacity(self):
        for i in range(50):
            self.entry(str(i))
        a.trim(self.c, 10000)
        count = self.c.execute("SELECT count(*) FROM watchlist WHERE status='active'").fetchone()[0]
        self.assertEqual(count, 36)

    def test_detail_budget_and_fairness(self):
        for i in range(36):
            self.entry(str(i), 10000+i)
        due = a.due(self.c, 10100, {'0'})
        self.assertEqual(len(due), 12)
        self.assertEqual(due[0], '1')
        self.assertNotIn('0', due)

    def test_stale_old_coin_not_admitted(self):
        self.sample(ts=9000)
        a.admit(self.c, 'x', 'revival', 10000, 'rank')
        self.assertIsNone(self.row())

    def test_discovery_budget_and_board_cadence(self):
        calls = []
        def fetch(page, query, sort):
            calls.append((page, query, sort))
            coins = [{'mint': f'{sort}-{page}-{i}', 'createdAt': 9999-page*60-i}
                     for i in range(60)]
            return {'coins': coins, 'total': 40000}
        with patch('adaptive.time.time', return_value=10000), patch('adaptive.time.sleep'):
            a.run_cycle(self.c, fetch, lambda c, d: None)
            self.assertEqual(len(calls), 6)
            self.assertTrue(all(page <= 2 for page, _, _ in calls))
            self.assertEqual(a.get_state(self.c, 'boardEntrants'), 0)
            calls.clear()
            a.run_cycle(self.c, fetch, lambda c, d: None)
            self.assertEqual(calls, [(0, None, 'new')])

    def test_rank_event_admits_old_coin(self):
        self.c.execute('INSERT INTO leaderboard VALUES (?,?)', ('old', 50))
        def fetch(page, query, sort):
            coins = [{'mint': 'old', 'createdAt': 1}] if sort == 'volume24h' and page == 0 else []
            return {'coins': coins, 'total': 1}
        def ingest(c, data):
            for coin in data['coins']:
                self.sample(coin['mint'], 200000)
        with patch('adaptive.time.time', return_value=200000), patch('adaptive.time.sleep'):
            a.run_cycle(self.c, fetch, ingest)
        self.assertEqual(self.row('old')['kind'], 'revival')
        self.assertEqual(self.row('old')['status'], 'active')


if __name__ == '__main__':
    unittest.main()
