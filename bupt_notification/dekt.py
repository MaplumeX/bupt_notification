"""第二课堂 API 客户端（纯 HTTP，仅依赖标准库）。

关键事实（实测确认）：
- 认证方式：请求头 `Authorization: Bearer <token>`，token 存在浏览器
  localStorage 的 `secondclass.tokenv3`。
- 通知列表：POST /api/v1/news/search
    body: {"type": "notification", "size": N, "offset": M, "show_details": false}
    返回 {"code":200,"data":{"hits":[{id,title,author,time,content,link,...}],"total":N}}
- 详情：GET /api/v1/news/<news_id>/details
- 站点前置了瑞数类 WAF，但带上 Bearer token 的普通 HTTP 请求可以正常通行；
  只有「登录换 token」必须走浏览器（验证码是 JS 生成的）。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

log = logging.getLogger("bupt.dekt")

# 接口不是真过滤，取多少原始条目：按需要的通知条数放大若干倍再截断
RAW_FETCH_FACTOR = 4
RAW_FETCH_MAX = 300


class AuthError(RuntimeError):
    """token 缺失 / 失效，需要重新登录。"""


class ApiError(RuntimeError):
    """其它接口错误（网络、5xx、返回体异常）。"""


# ---------- token（JWT）解析：用于提前续期 ----------

def jwt_payload(token: str) -> dict:
    """不校验签名地取出 JWT payload；不是 JWT 就返回 {}。"""
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return {}
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        data = base64.urlsafe_b64decode(pad)
        payload = json.loads(data)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def token_expires_at(token: str) -> int | None:
    exp = jwt_payload(token).get("exp")
    try:
        return int(exp) if exp else None
    except (TypeError, ValueError):
        return None


def token_seconds_left(token: str) -> int | None:
    """剩余有效期（秒）；无法解析返回 None。"""
    exp = token_expires_at(token)
    if exp is None:
        return None
    return exp - int(time.time())


def token_holder(token: str) -> str:
    """从 token 里取学号，仅用于日志（永远不要打印 token 本身）。"""
    sub = jwt_payload(token).get("sub")
    return str(sub) if sub else "?"


def _iso_to_local(iso: str) -> str:
    """'2026-09-18T19:53:03+08:00' -> '2026-09-18 19:53'"""
    if not iso:
        return ""
    s = iso.replace("T", " ")
    return s[:16]


def normalize(hit: dict[str, Any], api_base: str) -> dict[str, Any]:
    """把接口返回的单条通知整理成内部结构。"""
    news_id = int(hit.get("id"))
    section = hit.get("section") or []
    return {
        "id": news_id,
        "title": (hit.get("title") or "").strip(),
        "author": (hit.get("author") or "").strip(),
        "time": hit.get("time") or "",
        "time_local": _iso_to_local(hit.get("time") or ""),
        # 频道：/api/v1/news/search 的 type 参数并不是真过滤，返回的是混合流，
        # 真正的频道在每条记录的 type 字段（其次 section[0]）里。
        "channel": (hit.get("type") or (section[0] if section else "") or "").strip(),
        "section": section,
        "content": (hit.get("content") or "").strip(),
        "url": f"{api_base}/news/{news_id}",
        "source_url": hit.get("link") or "",
    }


def item_channel(item: dict) -> str:
    """取条目频道：优先 channel 字段，其次 section[0]（兼容旧数据）。"""
    chan = item.get("channel")
    if chan:
        return str(chan).strip()
    section = item.get("section") or []
    return str(section[0]).strip() if section else ""


def filter_channels(items: list[dict], channels: Sequence[str]) -> list[dict]:
    """只保留指定频道的条目；channels 为空表示不过滤。"""
    wanted = {c.strip() for c in channels if c and c.strip()}
    if not wanted:
        return list(items)
    return [it for it in items if item_channel(it) in wanted]



class DektClient:
    def __init__(self, api_base: str, token: str, timeout: int = 25, user_agent: str = "Mozilla/5.0"):
        self.api_base = api_base.rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.user_agent = user_agent

    # ---------- 底层 ----------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        form: dict | None = None,
        auth: bool = True,
    ) -> dict:
        url = f"{self.api_base}{path}"
        data: bytes | None = None
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{self.api_base}/",
        }
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if auth:
            if not self.token:
                raise AuthError("本地没有 token，需要先登录")
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        except urllib.error.URLError as exc:
            raise ApiError(f"网络错误: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(f"返回非 JSON（HTTP {status}）: {raw[:200]}")

        code = int(payload.get("code") or status or 0)
        if code in (401, 403) or payload.get("message") in (
            "Missing Token Or Invalid Token", "Invalid Token",
        ):
            raise AuthError(payload.get("message") or "token 失效")
        if code >= 400 or payload.get("status") == "error" or payload.get("err"):
            msg = payload.get("err") or payload.get("error") or payload.get("message") or raw[:200]
            raise ApiError(f"接口错误 HTTP {status}: {msg}")
        return payload

    # ---------- 业务 ----------
    def search_notifications(self, size: int = 50, offset: int = 0,
                             channels: Sequence[str] = ()) -> list[dict]:
        """按发布时间倒序返回通知列表。

        注意：接口的 type=notification 不是真过滤，返回的是「通知+新闻+公告+讲座」
        的混合流，所以要多取一些原始条目再按频道过滤，避免被新闻挤掉通知。
        """
        raw_size = min(RAW_FETCH_MAX, max(size * RAW_FETCH_FACTOR, size))
        payload = self._request(
            "POST",
            "/api/v1/news/search",
            json_body={
                "type": "notification",
                "size": int(raw_size),
                "offset": int(offset),
                "show_details": False,
            },
        )
        data = payload.get("data") or {}
        if isinstance(data, str):  # 接口偶尔把 data 序列化成字符串
            data = json.loads(data)
        hits = data.get("hits") or []
        items = [normalize(h, self.api_base) for h in hits if h.get("id")]
        return filter_channels(items, channels)[:size]

    def notification_total(self) -> int:
        payload = self._request(
            "POST",
            "/api/v1/news/search",
            json_body={"type": "notification", "size": 1, "offset": 0, "show_details": False},
        )
        data = payload.get("data") or {}
        if isinstance(data, str):
            data = json.loads(data)
        return int(data.get("total") or 0)

    def notification_detail(self, news_id: int) -> dict:
        payload = self._request("GET", f"/api/v1/news/{int(news_id)}/details")
        data = payload.get("data") or {}
        return normalize(data, self.api_base)

    def whoami(self) -> dict:
        payload = self._request("GET", "/api/v1/role/my")
        return payload.get("data") or {}
