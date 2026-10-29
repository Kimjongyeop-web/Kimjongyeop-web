"""Synthetic checks only; credentials and network are not needed."""
import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
import datetime as dt
from types import SimpleNamespace
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot as new


def blocked(*args, **kwargs):
    raise AssertionError("Tests must not access credentials or the network")


_patches = [patch("bot.load_dotenv", side_effect=blocked),
            patch("requests.sessions.Session.request", side_effect=blocked)]


def setUpModule():
    for item in _patches:
        item.start()


def tearDownModule():
    for item in reversed(_patches):
        item.stop()


class Tests(unittest.TestCase):
    def rows(self,n=12):
        return pd.DataFrame([{'time':f'09{i:02d}','open':100+i,'high':102+i,'low':99+i,'close':101+i,'volume':10+i} for i in range(n)])
    def test_exact_ohlcv(self):
        bars,meta=new.build_three_minute_bars(self.rows(),'20261029','0912')
        self.assertEqual(len(bars),4)
        self.assertEqual(bars.iloc[0].to_dict(),dict(open=100,high=104,low=99,close=103,volume=33))
        self.assertTrue(new.three_minute_check(self.rows(),'20261029','0912')[0])
    def test_open_bucket_excluded(self):
        bars,_=new.build_three_minute_bars(self.rows(),'20261029','0911')
        self.assertEqual(len(bars),3)
        self.assertEqual(bars.index[-1].strftime('%H%M'),'0906')
    def test_missing_minute(self):
        rows=self.rows(9).drop(index=4)
        bars,meta=new.build_three_minute_bars(rows,'20261029','0909')
        self.assertEqual(len(bars),2);self.assertEqual(meta['incomplete'],1)
        self.assertIn('3분봉 부족',new.three_minute_check(rows,'20261029','0909')[1])
    def test_bearish_distinct_from_missing(self):
        rows=self.rows(9);rows.loc[8,'close']=100
        ok,msg=new.three_minute_check(rows,'20261029','0909')
        self.assertFalse(ok);self.assertIn('추세 탈락',msg);self.assertIn('음봉/도지',msg)
        self.assertNotIn('3분봉 부족',msg)
    def test_bullish_below_average(self):
        rows=self.rows(9);rows.loc[0:5,'close']=120;rows.loc[6:8,'open']=100;rows.loc[8,'close']=101
        ok,msg=new.three_minute_check(rows,'20261029','0909')
        self.assertFalse(ok);self.assertIn('평균 미만',msg);self.assertNotIn('음봉/도지',msg)
    def test_duplicate_minute_not_three(self):
        rows=self.rows(9);rows=pd.concat([rows,rows.iloc[[4]]],ignore_index=True)
        bars,meta=new.build_three_minute_bars(rows,'20261029','0909')
        self.assertEqual(len(bars),2);self.assertEqual(meta['duplicates'],2)
    def test_invalid_and_wrong_day(self):
        rows=self.rows(9);rows.loc[0,'time']='oops';rows['date']='20261028'
        bars,meta=new.build_three_minute_bars(rows,'20261029','0909')
        self.assertTrue(bars.empty);self.assertEqual(meta['invalid'],9)
    def test_unsorted_same_result(self):
        a,_=new.build_three_minute_bars(self.rows(),'20261029','0912')
        b,_=new.build_three_minute_bars(self.rows().iloc[::-1],'20261029','0912')
        pd.testing.assert_frame_equal(a,b)
    def test_no_deprecation_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error',DeprecationWarning)
            new.three_minute_check(self.rows(),'20261029','0912')


class EntryAndStatusTests(unittest.TestCase):
    def test_entry_and_risk_configuration(self):
        cfg = new.Config("test", "test", "test")
        self.assertEqual(cfg.breakout_minutes, 10)
        self.assertEqual((cfg.stop_loss_pct, cfg.take_profit_pct, cfg.max_open_positions), (.02, .035, 6))
        self.assertIsNone(new.time_profile(dt.datetime(2026, 1, 5, 15, 10)))
        self.assertEqual(new.time_profile(dt.datetime(2026, 1, 5, 14))["body"], .001)

    def test_reception_counts_only_current_targets(self):
        now = dt.datetime(2026, 1, 5, 10, tzinfo=new.KST)
        bot = SimpleNamespace(desired_codes=["000001"], ticks_received={"000001": now, "000002": now},
                              positions={}, realized_pnl=0, check_status={})
        self.assertIn("수신 1/1", new.status_line(bot, now))
        bot.ticks_received["000001"] = now - dt.timedelta(minutes=4)
        self.assertIn("수신 확인 필요", new.status_line(bot, now))


if __name__ == "__main__":
    unittest.main()
