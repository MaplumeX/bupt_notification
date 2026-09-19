"""广播 / 待发队列的行为测试（纯标准库 unittest，不需要联网）。

运行：
    cd /root/bupt_notification
    python3 -m unittest discover -s tests -v
或容器内：
    docker compose exec -T bupt-notification python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bupt_notification.config import Config  # noqa: E402
from bupt_notification.monitor import Monitor  # noqa: E402
from bupt_notification.state import State  # noqa: E402
from bupt_notification.telegram import TelegramError  # noqa: E402


def item(nid: int, title: str = "测试通知") -> dict:
    return {
        "id": nid, "title": title, "author": "测试单位",
        "time": "2026-09-19T10:00:00+08:00", "time_local": "2026-09-19 10:00",
        "content": "", "section": [], "url": f"https://example.test/news/{nid}",
        "source_url": "",
    }


class BroadcastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            bot_token="123:TEST", chat_id="",
            state_file=root / "state.json", lock_file=root / "monitor.lock",
            browser_state_file=root / "browser.json", page_size=5,
        )
        self.state = State(self.cfg.state_file)
        self.mon = Monitor(self.cfg, self.state)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- broadcast ----------
    def test_broadcast_counts_each_subscriber(self) -> None:
        self.state.add_subscriber("111")
        self.state.add_subscriber("222")
        with mock.patch("bupt_notification.monitor.send_message", return_value={}) as m:
            self.assertEqual(self.mon.broadcast("hi"), 2)
        self.assertEqual([c.args[1] for c in m.call_args_list], ["111", "222"])

    def test_broadcast_without_subscribers_returns_zero(self) -> None:
        with mock.patch("bupt_notification.monitor.send_message") as m:
            self.assertEqual(self.mon.broadcast("hi"), 0)
        m.assert_not_called()

    def test_blocked_subscriber_is_removed(self) -> None:
        self.state.add_subscriber("111")
        self.state.add_subscriber("222")

        def fake(token, chat_id, text, **kw):
            if chat_id == "111":
                raise TelegramError("Forbidden: bot was blocked by the user", code=403)
            return {}

        with mock.patch("bupt_notification.monitor.send_message", side_effect=fake):
            self.assertEqual(self.mon.broadcast("hi"), 1)
        self.assertEqual(self.state.subscriber_ids(), ["222"])

    def test_all_failures_raise(self) -> None:
        self.state.add_subscriber("111")
        with mock.patch("bupt_notification.monitor.send_message",
                        side_effect=TelegramError("网络错误: timed out")):
            with self.assertRaises(TelegramError):
                self.mon.broadcast("hi")

    # ---------- flush_pending ----------
    def test_flush_pushes_and_records(self) -> None:
        self.state.add_subscriber("111")
        self.state.queue([item(1001)], self.cfg.max_pending)
        with mock.patch("bupt_notification.monitor.send_message", return_value={}):
            self.assertEqual(self.mon.flush_pending(), 1)
        self.assertEqual(self.state.pending, [])
        self.assertIn(1001, self.state.seen_ids())

    def test_flush_keeps_item_when_zero_delivered(self) -> None:
        """回归：唯一订阅者在本轮被拉黑移除时，零送达必须保留队列，不能记账丢通知。"""
        self.state.add_subscriber("111")
        self.state.queue([item(1002)], self.cfg.max_pending)
        with mock.patch("bupt_notification.monitor.send_message",
                        side_effect=TelegramError("Forbidden: bot was blocked", code=403)):
            self.assertEqual(self.mon.flush_pending(), 0)
        self.assertEqual([i["id"] for i in self.state.pending], [1002])
        self.assertNotIn(1002, self.state.seen_ids())

    def test_flush_skips_when_paused(self) -> None:
        self.state.add_subscriber("111")
        self.state.queue([item(1003)], self.cfg.max_pending)
        self.state.data["paused"] = True
        with mock.patch("bupt_notification.monitor.send_message") as m:
            self.assertEqual(self.mon.flush_pending(), 0)
        m.assert_not_called()
        self.assertEqual([i["id"] for i in self.state.pending], [1003])

    def test_flush_skips_without_token(self) -> None:
        self.cfg.bot_token = ""
        self.state.add_subscriber("111")
        self.state.queue([item(1004)], self.cfg.max_pending)
        with mock.patch("bupt_notification.monitor.send_message") as m:
            self.assertEqual(self.mon.flush_pending(), 0)
        m.assert_not_called()
        self.assertEqual([i["id"] for i in self.state.pending], [1004])

    # ---------- dry-run ----------
    def test_dry_run_marks_delivered_without_sending(self) -> None:
        self.state.add_subscriber("111")
        self.state.queue([item(1005)], self.cfg.max_pending)
        mon = Monitor(self.cfg, self.state, dry_run=True)
        with mock.patch("bupt_notification.monitor.send_message") as m:
            self.assertEqual(mon.flush_pending(), 1)
        m.assert_not_called()
        self.assertIn(1005, self.state.seen_ids())


class SubscriberStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_remove_dedup(self) -> None:
        self.assertTrue(self.state.add_subscriber("111"))
        self.assertFalse(self.state.add_subscriber("111"))
        self.state.add_subscriber(222)  # 数字也接受
        self.assertEqual(self.state.subscriber_ids(), ["111", "222"])
        self.assertTrue(self.state.remove_subscriber("111"))
        self.assertFalse(self.state.remove_subscriber("999"))
        self.assertEqual(self.state.subscriber_ids(), ["222"])

    def test_reset_keeps_subscribers_and_token(self) -> None:
        self.state.add_subscriber("111")
        self.state.set_token("tok")
        self.state.data["seen"] = [1, 2, 3]
        self.state.reset()
        self.assertEqual(self.state.subscriber_ids(), ["111"])
        self.assertEqual(self.state.token, "tok")
        self.assertEqual(self.state.data["seen"], [])
        self.assertFalse(self.state.baseline_done)

    def test_paused_flag(self) -> None:
        self.assertFalse(self.state.paused)
        self.state.data["paused"] = True
        self.assertTrue(self.state.paused)


if __name__ == "__main__":
    unittest.main(verbosity=2)
