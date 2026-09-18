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

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("bupt.dekt")


class AuthError(RuntimeError):
    """token 缺失 / 失效，需要重新登录。"""


class ApiError(RuntimeError):
    """其它接口错误（网络、5xx、返回体异常）。"""


def _iso_to_local(iso: str) -> str:
    """'2026-09-18T19:53:03+08:00' -> '2026-09-18 19:53'"""
    if not iso:
        return ""
    s = iso.replace("T", " ")
    return s[:16]


def normalize(hit: dict[str, Any], api_base: str) -> dict[str, Any]:
    """把接口返回的单条通知整理成内部结构。"""
    news_id = int(hit.get("id"))
    return {
        "id": news_id,
        "title": (hit.get("title") or "").strip(),
        "author": (hit.get("author") or "").strip(),
        "time": hit.get("time") or "",
        "time_local": _iso_to_local(hit.get("time") or ""),
        "section": hit.get("section") or [],
        "content": (hit.get("content") or "").strip(),
        "url": f"{api_base}/news/{news_id}",
        "source_url": hit.get("link") or "",
    }


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
    def search_notifications(self, size: int = 50, offset: int = 0) -> list[dict]:
        """按发布时间倒序返回通知列表。"""
        payload = self._request(
            "POST",
            "/api/v1/news/search",
            json_body={
                "type": "notification",
                "size": int(size),
                "offset": int(offset),
                "show_details": False,
            },
        )
        data = payload.get("data") or {}
        if isinstance(data, str):  # 接口偶尔把 data 序列化成字符串
            data = json.loads(data)
        hits = data.get("hits") or []
        return [normalize(h, self.api_base) for h in hits if h.get("id")]

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
