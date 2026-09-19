"""Telegram 命令机器人：/start /stop /status /pause /resume /pull /latest /subscribers /help。

公开订阅模式：任何人 /start 即可订阅推送；/stop 退订。
控制型命令（/pause /resume /pull /subscribers）仅限管理员
（TELEGRAM_CHAT_ID 配置的那个会话）。

监控主循环在等待下一轮检查的间隙里长轮询 getUpdates，
把收到的命令分发到对应处理函数。
"""

from __future__ import annotations

import html
import logging

from . import __version__
from .config import Config
from .dekt import token_seconds_left
from .monitor import Monitor, fetch_latest
from .state import State
from .telegram import TelegramError, get_updates, send_message, set_my_commands

log = logging.getLogger("bupt.bot")

LATEST_DEFAULT = 10     # /latest 默认列 10 条
LATEST_MAX = 30         # 单次最多列 30 条
MSG_LIMIT = 4000        # Telegram 单条消息上限 4096，留点余量

COMMANDS_MENU: list[tuple[str, str]] = [
    ("start", "订阅通知推送"),
    ("stop", "退订推送"),
    ("status", "查看监控状态"),
    ("pause", "暂停推送（管理员）"),
    ("resume", "恢复推送并补发队列（管理员）"),
    ("pull", "立即拉取检查一轮（管理员）"),
    ("latest", "列出最新10条通知"),
    ("subscribers", "查看订阅情况（管理员）"),
    ("help", "查看全部命令"),
]

ADMIN_COMMANDS = {"pause", "resume", "pull", "subscribers"}

WELCOME = (
    "👋 <b>北邮第二课堂通知监控</b>\n"
    "已为你开启订阅：有新的校内通知会立刻推送到这里。\n\n"
    "常用命令：/status 查看状态 · /latest 看最新通知 · /stop 退订"
)


def status_text(cfg: Config, state: State) -> str:
    """渲染 /status 的回复文本（Telegram HTML）。"""
    d = state.data
    token = state.token or cfg.token
    if not token:
        token_desc = "无"
    else:
        left = token_seconds_left(token)
        if left is None:
            token_desc = "有（无法预判有效期）"
        elif left <= 0:
            token_desc = f"已过期 {abs(left) // 3600} 小时（下轮自动重登）"
        else:
            token_desc = f"剩余 {left / 3600:.1f} 小时"
    stats = d.get("stats") or {}
    lines = [
        "📊 <b>监控状态</b>",
        f"推送：{'⏸ 已暂停' if state.paused else '▶️ 运行中'}",
        f"版本：{__version__}",
        f"订阅者：{len(state.subscriber_ids())} 人",
        f"基线：{'已建立' if state.baseline_done else '未建立'}",
        f"已记录通知：{len(d.get('seen', []))} 条",
        f"待发队列：{len(state.pending)} 条",
        f"token：{token_desc}",
        f"账号：{'已配置（可自动续期）' if (cfg.username and cfg.password) else '未配置'}",
        f"上次检查：{d.get('last_check_iso') or '还没跑过'}",
        f"检查频率：每 {cfg.interval_minutes} 分钟",
        f"统计：运行 {stats.get('runs', 0)} 轮 / 推送 {stats.get('pushed', 0)} 条 / 出错 {stats.get('errors', 0)} 次",
    ]
    return "\n".join(lines)


def latest_text(items: list[dict]) -> str:
    """把通知列表渲染成 /latest 的回复文本。"""
    lines = [f"📋 <b>最新 {len(items)} 条通知</b>", ""]
    for i, it in enumerate(items, 1):
        title = html.escape(it.get("title") or "(无标题)")
        when = html.escape(it.get("time_local") or it.get("time") or "")
        url = it.get("url") or ""
        line = f"{i}. <b>{title}</b>"
        if when:
            line += f"（{when}）"
        if url:
            line += f'\n   🔗 {html.escape(url)}'
        lines.append(line)
    return "\n".join(lines)


class CommandBot:
    """长轮询 getUpdates 并分发命令。offset 持久化在状态文件里，重启后不重放旧命令。"""

    def __init__(self, cfg: Config, state: State, monitor: Monitor):
        self.cfg = cfg
        self.state = state
        self.monitor = monitor
        self.offset = int(state.data.get("tg_update_offset") or 0)

    # ---------- 基础 ----------
    def reply(self, chat_id: str, text: str) -> None:
        for i in range(0, max(len(text), 1), MSG_LIMIT):
            try:
                send_message(self.cfg.bot_token, chat_id, text[i:i + MSG_LIMIT],
                             timeout=self.cfg.timeout_seconds)
            except TelegramError as exc:
                log.warning("回复 %s 失败：%s", chat_id, exc)
                return

    def register_commands(self) -> None:
        try:
            set_my_commands(self.cfg.bot_token, COMMANDS_MENU)
        except TelegramError as exc:
            log.warning("注册命令菜单失败（不影响使用）：%s", exc)

    def _save_offset(self) -> None:
        if self.offset != int(self.state.data.get("tg_update_offset") or 0):
            self.state.data["tg_update_offset"] = self.offset
            self.state.save()

    # ---------- 轮询入口 ----------
    def poll_and_handle(self, timeout: int = 25) -> int:
        """长轮询一批 updates 并处理其中的命令。返回本次处理的命令数。"""
        try:
            updates = get_updates(
                self.cfg.bot_token,
                offset=self.offset or None,
                poll_timeout=max(0, int(timeout)),
                timeout=max(0, int(timeout)) + self.cfg.timeout_seconds,
            )
        except TelegramError as exc:
            log.warning("getUpdates 失败：%s", exc)
            return 0
        if not isinstance(updates, list) or not updates:
            return 0
        handled = 0
        for upd in updates:
            self.offset = max(self.offset, int(upd.get("update_id") or 0) + 1)
            msg = upd.get("message") or upd.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = str((msg.get("chat") or {}).get("id") or "")
            if not text or not chat_id:
                continue
            self.handle(chat_id, text)
            handled += 1
        self._save_offset()
        return handled

    # ---------- 分发 ----------
    def handle(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].split("@")[0].lower().lstrip("/")  # 去掉 / 前缀和 @botname 后缀
        is_admin = bool(self.cfg.chat_id) and chat_id == self.cfg.chat_id
        handlers = {
            "start": self.cmd_start,
            "stop": self.cmd_stop,
            "help": self.cmd_help,
            "status": self.cmd_status,
            "pause": self.cmd_pause,
            "resume": self.cmd_resume,
            "pull": self.cmd_pull,
            "latest": self.cmd_latest,
            "list": self.cmd_latest,  # 别名
            "subscribers": self.cmd_subscribers,
        }
        handler = handlers.get(cmd)
        if handler is None:
            self.reply(chat_id, f"未知命令 /{html.escape(cmd)}。可用命令见 /help")
            return
        if cmd in ADMIN_COMMANDS and not is_admin:
            if not self.cfg.chat_id:
                self.reply(chat_id, f"🔒 /{cmd} 是管理员命令，但本服务未配置管理员（TELEGRAM_CHAT_ID）。")
            else:
                self.reply(chat_id, f"🔒 /{cmd} 是管理员命令。")
            return
        log.info("处理命令：/%s（chat %s%s）", cmd, chat_id, "，管理员" if is_admin else "")
        try:
            handler(chat_id, text)
        except Exception as exc:  # noqa: BLE001  # 命令失败不能拖垮主循环
            log.exception("命令 /%s 处理异常", cmd)
            self.reply(chat_id, f"❌ 命令执行出错：{html.escape(str(exc))}")

    # ---------- 各命令 ----------
    def cmd_start(self, chat_id: str, text: str) -> None:
        if chat_id in self.state.subscriber_ids():
            self.reply(chat_id, "你已经订阅过了，有新通知会立刻推给你。")
            return
        self.state.add_subscriber(chat_id)
        self.state.save()
        queued = len(self.state.pending)
        msg = WELCOME
        if queued and not self.state.paused:
            # 新订阅者赶上了积压：立即补发队列（广播，老订阅者去重后无感）
            sent = self.monitor.flush_pending()
            msg += f"\n\n📥 待发队列里有 {queued} 条积压通知，已补发 {sent} 条。"
        elif queued:
            msg += f"\n\n⏸ 推送处于暂停状态，{queued} 条通知排队中，/resume（管理员）后补发。"
        self.reply(chat_id, msg)

    def cmd_stop(self, chat_id: str, text: str) -> None:
        if self.state.remove_subscriber(chat_id):
            self.state.save()
            self.reply(chat_id, "已退订，不会再收到推送。随时 /start 重新订阅。")
        else:
            self.reply(chat_id, "你本来就没有订阅。")

    def cmd_help(self, chat_id: str, text: str) -> None:
        is_admin = bool(self.cfg.chat_id) and chat_id == self.cfg.chat_id
        lines = ["🤖 <b>北邮第二课堂通知监控</b>", ""]
        lines.extend(f"/{cmd} — {desc}" for cmd, desc in COMMANDS_MENU)
        lines += ["", "任何人都可 /start 订阅、/stop 退订；监控自动推送新通知。"]
        if not is_admin:
            lines.append("标有「管理员」的命令仅限服务维护者使用。")
        self.reply(chat_id, "\n".join(lines))

    def cmd_subscribers(self, chat_id: str, text: str) -> None:
        subs = self.state.subscriber_ids()
        lines = [f"👥 <b>订阅者：{len(subs)} 人</b>"]
        for i, s in enumerate(subs, 1):
            tag = " ⭐️（管理员）" if self.cfg.chat_id and s == self.cfg.chat_id else ""
            lines.append(f"{i}. <code>{html.escape(s)}</code>{tag}")
        self.reply(chat_id, "\n".join(lines))

    def cmd_status(self, chat_id: str, text: str) -> None:
        self.reply(chat_id, status_text(self.cfg, self.state))

    def cmd_pause(self, chat_id: str, text: str) -> None:
        if self.state.paused:
            self.reply(chat_id, "⏸ 推送已经处于暂停状态。")
            return
        self.state.data["paused"] = True
        self.state.save()
        queued = len(self.state.pending)
        msg = "⏸ <b>已暂停推送</b>（对所有订阅者生效）"
        msg += f"\n待发队列里还有 {queued} 条通知，/resume 后会补发。" if queued else "\n当前队列为空，新通知会照常入队。"
        self.reply(chat_id, msg)

    def cmd_resume(self, chat_id: str, text: str) -> None:
        if not self.state.paused:
            self.reply(chat_id, "▶️ 推送本来就在运行中，无需恢复。")
            return
        self.state.data["paused"] = False
        self.state.save()
        sent = self.monitor.flush_pending()
        queued = len(self.state.pending)
        msg = f"▶️ <b>已恢复推送</b>\n补发 {sent} 条，队列剩余 {queued} 条。"
        self.reply(chat_id, msg)

    def cmd_pull(self, chat_id: str, text: str) -> None:
        self.reply(chat_id, "🔄 正在立即拉取检查…")
        try:
            res = self.monitor.run_once_locked()
        except RuntimeError as exc:
            self.reply(chat_id, f"⚠️ {html.escape(str(exc))}")
            return
        if res["error"]:
            self.state.bump("errors")
            self.state.save()
            self.reply(chat_id, f"❌ 检查失败：{html.escape(res['error'])}")
            return
        msg = (
            f"✅ <b>检查完成</b>\n"
            f"抓取 {res['fetched']} 条 / 新增 {res['new']} 条 / 推送 {res['pushed']} 条"
            f" / 队列剩余 {res['pending']} 条"
        )
        if res["baseline"]:
            msg += "\n（本轮建立基线，未推送历史通知）"
        if self.state.paused:
            msg += "\n⏸ 当前处于暂停状态，新通知已入队，/resume 后补发。"
        self.reply(chat_id, msg)

    def cmd_latest(self, chat_id: str, text: str) -> None:
        n = LATEST_DEFAULT
        parts = text.split()
        if len(parts) > 1:
            try:
                n = max(1, min(LATEST_MAX, int(parts[1])))
            except ValueError:
                pass
        self.reply(chat_id, f"📋 正在拉取最新 {n} 条通知…")
        try:
            items = fetch_latest(self.cfg, limit=n, token=self.state.token or self.cfg.token)
        except Exception as exc:  # noqa: BLE001
            log.warning("/latest 拉取失败：%s", exc)
            self.reply(chat_id, f"❌ 拉取失败：{html.escape(str(exc))}")
            return
        if not items:
            self.reply(chat_id, "没有拉到通知。")
            return
        self.reply(chat_id, latest_text(items))
