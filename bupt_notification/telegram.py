"""Telegram 推送（Bot API，标准库实现，无第三方依赖）。"""

from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("bupt.telegram")

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


def _call(bot_token: str, method: str, params: dict, timeout: int = 20) -> dict:
    if not bot_token:
        raise TelegramError("没有配置 TELEGRAM_BOT_TOKEN")
    url = f"{API}/bot{bot_token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            raise TelegramError(f"HTTP {exc.code}: {raw[:200]}") from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"网络错误: {exc.reason}") from exc

    if not payload.get("ok"):
        raise TelegramError(payload.get("description") or f"{method} 调用失败")
    return payload.get("result") or {}


def send_message(bot_token: str, chat_id: str, text: str, *, disable_preview: bool = True, timeout: int = 20) -> dict:
    return _call(bot_token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false",
    }, timeout=timeout)


def get_me(bot_token: str) -> dict:
    return _call(bot_token, "getMe", {})


def detect_chat_id(bot_token: str) -> str:
    """从 getUpdates 里找出最近给你发消息的 chat_id。

    用法：先给机器人发一条任意消息（如 /start），再运行检测。
    """
    updates = _call(bot_token, "getUpdates", {"limit": 100, "timeout": 0})
    if not isinstance(updates, list):
        return ""
    for upd in reversed(updates):
        for key in ("message", "edited_message", "channel_post", "callback_query"):
            node = upd.get(key) or {}
            chat = node.get("chat") or {}
            if chat.get("id"):
                return str(chat["id"])
    return ""


def format_notice(item: dict, include_content: bool = False) -> str:
    """把一条通知渲染成 Telegram HTML 文本。"""
    title = html.escape(item.get("title") or "(无标题)")
    author = html.escape(item.get("author") or "")
    when = html.escape(item.get("time_local") or item.get("time") or "")
    url = item.get("url") or ""

    lines = ["📢 <b>校内通知</b>"]
    lines.append(f"<b>{title}</b>")
    meta = " · ".join(x for x in (author, when) if x)
    if meta:
        lines.append(meta)
    if include_content and item.get("content"):
        body = item["content"]
        if len(body) > 1200:
            body = body[:1200] + "…"
        lines.append("")
        lines.append(html.escape(body))
    if url:
        lines.append("")
        lines.append(f'🔗 <a href="{html.escape(url, quote=True)}">查看原文</a>')
    return "\n".join(lines)


def format_summary(count: int) -> str:
    return f"✅ <b>北邮校内通知监控已启用</b>\n本周期补推 {count} 条通知。"


def format_start(count: int, interval_minutes: int) -> str:
    return (
        "🤖 <b>北邮第二课堂通知监控已启动</b>\n"
        f"当前基线：{count} 条通知（不推送历史）\n"
        f"检查频率：每 {interval_minutes} 分钟\n"
        "有新的校内通知会立刻推给你。"
    )
