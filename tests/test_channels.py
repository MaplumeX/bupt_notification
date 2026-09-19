"""频道过滤与待发队列清理的测试。

背景：`POST /api/v1/news/search` 的 `type` 参数**不是真过滤**，返回的是
「校内通知 + 校内新闻 + 全部公示公告 + 学术讲座」的混合流；真正的频道在每条
记录的 `type` 字段（其次 `section[0]`）里。默认只保留「校内通知」。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bupt_notification.config import Config, _get_channels  # noqa: E402
from bupt_notification.dekt import filter_channels, item_channel, normalize  # noqa: E402
from bupt_notification.monitor import Monitor  # noqa: E402
from bupt_notification.state import State  # noqa: E402

API = "https://dekt.bupt.edu.cn"


def hit(nid: int, channel: str, title: str = "标题") -> dict:
    """模拟接口返回的一条记录：频道在 type 字段，同时出现在 section[0]。"""
    return {
        "id": nid, "title": title, "author": "某单位",
        "time": "2026-09-19T10:00:00+08:00",
        "type": channel, "section": [channel, "党团行政", "某单位"],
        "content": "", "link": "",
    }


class NormalizeTests(unittest.TestCase):
    def test_channel_from_type_field(self) -> None:
        it = normalize(hit(1, "校内通知"), API)
        self.assertEqual(it["channel"], "校内通知")
        self.assertEqual(it["url"], f"{API}/news/1")

    def test_channel_falls_back_to_section(self) -> None:
        raw = hit(2, "校内新闻")
        raw.pop("type")  # 有些接口变体不带 type
        self.assertEqual(normalize(raw, API)["channel"], "校内新闻")


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [normalize(hit(i, c), API) for i, c in
                      [(1, "校内通知"), (2, "校内新闻"), (3, "全部公示公告"), (4, "学术讲座"), (5, "校内通知")]]

    def test_default_keeps_only_notices(self) -> None:
        kept = filter_channels(self.items, ("校内通知",))
        self.assertEqual([i["id"] for i in kept], [1, 5])

    def test_multiple_channels(self) -> None:
        kept = filter_channels(self.items, ("校内通知", "学术讲座"))
        self.assertEqual([i["id"] for i in kept], [1, 4, 5])

    def test_empty_means_no_filter(self) -> None:
        self.assertEqual(len(filter_channels(self.items, ())), 5)

    def test_legacy_item_without_channel_uses_section(self) -> None:
        legacy = {"id": 9, "title": "旧数据", "section": ["校内通知", "后勤部"]}
        self.assertEqual(item_channel(legacy), "校内通知")
        self.assertEqual([i["id"] for i in filter_channels([legacy], ("校内通知",))], [9])

    def test_item_with_unknown_channel_is_dropped(self) -> None:
        self.assertEqual(filter_channels([{"id": 10, "section": []}], ("校内通知",)), [])


class ConfigChannelTests(unittest.TestCase):
    """注意：这些用例必须与运行环境隔离 —— 容器里 NOTIFY_CHANNELS 是真实存在的环境变量，
    而 os.environ 的优先级高于传入的 env 字典（这是设计如此）。"""

    def setUp(self) -> None:
        self._saved = os.environ.pop("NOTIFY_CHANNELS", None)

    def tearDown(self) -> None:
        os.environ.pop("NOTIFY_CHANNELS", None)
        if self._saved is not None:
            os.environ["NOTIFY_CHANNELS"] = self._saved

    def test_missing_key_uses_default(self) -> None:
        self.assertNotIn("NOTIFY_CHANNELS", os.environ)
        self.assertEqual(_get_channels({}, "NOTIFY_CHANNELS", ("校内通知",)), ("校内通知",))

    def test_empty_value_means_no_filter(self) -> None:
        self.assertEqual(_get_channels({"NOTIFY_CHANNELS": ""}, "NOTIFY_CHANNELS", ("校内通知",)), ())

    def test_separators(self) -> None:
        for raw in ("校内通知,学术讲座", "校内通知，学术讲座", "校内通知、学术讲座"):
            self.assertEqual(_get_channels({"NOTIFY_CHANNELS": raw}, "NOTIFY_CHANNELS", ()),
                             ("校内通知", "学术讲座"))

    def test_env_overrides_file(self) -> None:
        os.environ["NOTIFY_CHANNELS"] = "学术讲座"
        self.assertEqual(_get_channels({"NOTIFY_CHANNELS": "校内通知"}, "NOTIFY_CHANNELS", ()),
                         ("学术讲座",))


class SearchFilteringTests(unittest.TestCase):
    """端到端（打桩 HTTP）：混合流里的新闻不应进入结果，且不会挤掉通知。"""

    def test_mixed_stream_is_filtered_and_padded(self) -> None:
        from bupt_notification.dekt import DektClient

        mixed = []
        for i in range(1, 41):
            mixed.append(hit(1000 + i, "校内新闻" if i % 2 else "校内通知"))
        client = DektClient(API, "tok")
        with mock.patch.object(DektClient, "_request", return_value={"code": 200, "data": {"hits": mixed}}):
            items = client.search_notifications(size=5, channels=("校内通知",))
        self.assertEqual(len(items), 5)
        self.assertTrue(all(i["channel"] == "校内通知" for i in items))
        self.assertEqual(items[0]["id"], 1002)  # 保持时间倒序（原始顺序）


class PruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(bot_token="1:T", chat_id="", state_file=root / "state.json",
                          lock_file=root / "lock", browser_state_file=root / "b.json")
        self.state = State(self.cfg.state_file)
        self.mon = Monitor(self.cfg, self.state, dry_run=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prune_drops_other_channels(self) -> None:
        self.state.queue([
            normalize(hit(1, "校内通知"), API),
            normalize(hit(2, "校内新闻"), API),
            {"id": 3, "title": "旧数据", "section": ["全部公示公告"]},   # 无 channel 字段
            {"id": 4, "title": "旧数据通知", "section": ["校内通知", "后勤部"]},
        ], self.cfg.max_pending)
        dropped = self.mon.prune_pending()
        self.assertEqual(dropped, 2)
        self.assertEqual(sorted(i["id"] for i in self.state.pending), [1, 4])
        # 已落盘
        self.assertEqual(sorted(i["id"] for i in State(self.cfg.state_file).pending), [1, 4])

    def test_prune_dry_run_does_not_persist(self) -> None:
        self.state.queue([normalize(hit(2, "校内新闻"), API)], self.cfg.max_pending)
        self.state.save()
        self.mon.prune_pending(persist=False)
        self.assertEqual(len(State(self.cfg.state_file).pending), 1)

    def test_prune_noop_when_queue_empty(self) -> None:
        self.assertEqual(self.mon.prune_pending(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
