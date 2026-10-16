import unittest
from monitor import signal

class SignalTests(unittest.TestCase):
    def rows(self):
        return [dict(ts=1000+i*100,price=p,holders=h,liquidity=6000) for i,(p,h) in enumerate([(1,20),(1.01,21),(1.03,23),(1.06,25)])]
    def test_confirmed(self):
        self.assertEqual(signal(self.rows(),1300)['gain'],6)
    def test_stale(self):
        self.assertIsNone(signal(self.rows(),1600))
    def test_insufficient_history(self):
        self.assertIsNone(signal(self.rows()[-2:],1300))
    def test_liquidity_loss(self):
        r=self.rows();r[-1]['liquidity']=5000
        self.assertIsNone(signal(r,1300))
    def test_overextended(self):
        r=self.rows();r[-1]['price']=2
        self.assertIsNone(signal(r,1300))
    def test_missing_holders(self):
        r=self.rows();r[-1]['holders']=None
        self.assertIsNone(signal(r,1300))

if __name__=='__main__':
    unittest.main()
