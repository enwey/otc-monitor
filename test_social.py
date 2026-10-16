import sqlite3
import unittest
from unittest.mock import patch
import monitor

class SocialTests(unittest.TestCase):
    def test_cache_and_missing_link(self):
        c=sqlite3.connect(':memory:')
        c.execute('CREATE TABLE coins(mint TEXT)')
        c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT)')
        mint='A'*32
        c.execute('INSERT INTO coins VALUES (?)',(mint,))
        with patch.object(monitor.radar.Sources,'coins',return_value={'coins':[{'mint':mint,'twitter':'https://x.com/example'}]}) as fetch:
            self.assertEqual(monitor.social_links(c,mint)['twitter'],'https://x.com/example')
            monitor.social_links(c,mint)
            self.assertEqual(fetch.call_count,1)
        with self.assertRaises(ValueError):monitor.social_links(c,'https://example.com')
        with self.assertRaises(ValueError):monitor.social_links(c,'B'*32)
        c.close()
