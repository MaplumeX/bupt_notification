"""状态持久化：基线 / 已推送 / 待发队列 / token。

文件是单个 JSON，写入用「临时文件 + 原子替换」，避免断电/并发写坏。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_SEEN = 2000  # 只保留最近 N 条已推送 id，防止文件无限增长


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "baseline_done": False,
            "seen": [],          # 已推送过的 news_id（最新的在后）
            "pending": [],       # 抓到了但还没成功推送的条目
            "token": "",
            "token_updated_at": 0,
            "last_check_at": 0,
            "last_check_iso": "",
            "started_notified": False,
            "stats": {"runs": 0, "pushed": 0, "errors": 0},
        }
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # 损坏的状态文件：备份后从零开始，绝不静默丢数据
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
            try:
                self.path.replace(backup)
            except Exception:
                pass
            return
        if isinstance(raw, dict):
            self.data.update(raw)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)  # 含 token，收紧权限
            except Exception:
                pass
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---------- 便捷访问 ----------
    @property
    def baseline_done(self) -> bool:
        return bool(self.data.get("baseline_done"))

    @property
    def pending(self) -> list[dict]:
        return self.data.setdefault("pending", [])

    @property
    def token(self) -> str:
        return self.data.get("token") or ""

    def set_token(self, token: str) -> None:
        self.data["token"] = token
        self.data["token_updated_at"] = int(time.time())

    # ---------- 行为 ----------
    def seen_ids(self) -> set[int]:
        return {int(i) for i in self.data.get("seen", [])}

    def pending_ids(self) -> set[int]:
        return {int(i["id"]) for i in self.pending if "id" in i}

    def mark_pushed(self, ids: list[int]) -> None:
        seen = [int(i) for i in self.data.get("seen", [])]
        seen.extend(int(i) for i in ids)
        # 去重同时保序
        deduped: list[int] = []
        present: set[int] = set()
        for i in reversed(seen):
            if i not in present:
                present.add(i)
                deduped.append(i)
        self.data["seen"] = list(reversed(deduped))[-MAX_SEEN:]

    def queue(self, items: list[dict], cap: int) -> None:
        pend = self.pending
        have = {int(i["id"]) for i in pend}
        for it in items:
            if int(it["id"]) in have:
                continue
            pend.append(it)
            have.add(int(it["id"]))
        if len(pend) > cap:  # 只保留最新的 cap 条
            self.data["pending"] = pend[-cap:]

    def drop_pending(self, item_id: int) -> None:
        self.data["pending"] = [i for i in self.pending if int(i["id"]) != int(item_id)]

    def touch_check(self) -> None:
        self.data["last_check_at"] = int(time.time())
        self.data["last_check_iso"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.data["stats"]["runs"] = int(self.data.get("stats", {}).get("runs", 0)) + 1

    def bump(self, key: str, delta: int = 1) -> None:
        stats = self.data.setdefault("stats", {})
        stats[key] = int(stats.get(key, 0)) + delta

    def reset(self) -> None:
        path = self.path
        token = self.token
        self.data = {
            "baseline_done": False, "seen": [], "pending": [], "token": token,
            "token_updated_at": self.data.get("token_updated_at", 0),
            "last_check_at": 0, "last_check_iso": "", "started_notified": False,
            "stats": {"runs": 0, "pushed": 0, "errors": 0},
        }
        self.save()
        assert self.path == path
