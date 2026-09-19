"""监控主循环：抓通知 → 去重 → 入队 → 推送 → 记录状态。"""

from __future__ import annotations

import fcntl
import logging
import random
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .dekt import ApiError, AuthError, DektClient, token_holder, token_seconds_left
from .state import State
from .telegram import TelegramError, format_notice, format_start, format_summary, send_message
from .web_login import LoginError, browser_login

log = logging.getLogger("bupt.monitor")


@contextmanager
def single_instance(lock_file: Path):
    """用 flock 防止定时任务与手动执行撞车。"""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_file, "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有另一个监控实例在运行（lock 被占用）")
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


class Monitor:
    def __init__(self, cfg: Config, state: State | None = None, *, dry_run: bool = False):
        self.cfg = cfg
        self.state = state or State(cfg.state_file)
        self.dry_run = dry_run
        # 管理员（TELEGRAM_CHAT_ID）自动成为首个订阅者，兼容旧部署
        if cfg.chat_id and not self.dry_run:
            if self.state.add_subscriber(cfg.chat_id):
                self.state.save()
        self.client = DektClient(
            cfg.api_base,
            self.state.token or cfg.token,
            timeout=cfg.timeout_seconds,
            user_agent=cfg.user_agent,
        )

    # ---------- 认证与续期 ----------
    def ensure_auth(self, force: bool = False) -> None:
        """保证 client.token 可用。

        - 没有 token  → 浏览器登录
        - token 是 JWT 且剩余不足 refresh_margin_hours → 提前续期（不等 401）
        - 其余情况 → 直接用；真正失效时由 _fetch_items 的 401 兜底重登
        """
        token = self.client.token
        if force or not token:
            reason = "强制刷新" if force else "本地无 token"
            log.info("需要登录（%s）", reason)
            self._relogin()
            return

        left = token_seconds_left(token)
        if left is None:
            return  # 不是标准 JWT，无法预判，交给 401 兜底
        if left <= 0:
            log.info("token 已过期（账号 %s），重新登录", token_holder(token))
            self._relogin()
        elif left < self.cfg.refresh_margin_hours * 3600:
            log.info("token 剩余 %.1f 小时（账号 %s），提前续期", left / 3600, token_holder(token))
            self._relogin()

    def _relogin(self) -> None:
        if not (self.cfg.username and self.cfg.password):
            raise AuthError(
                "没有可用 token，且未配置 BUPT_USERNAME / BUPT_PASSWORD，无法登录。"
                "请在 .env 里填好账号密码后重启。"
            )
        try:
            token = browser_login(
                self.cfg.api_base,
                self.cfg.username,
                self.cfg.password,
                chrome_path=self.cfg.chrome_path,
                browser_state_file=self.cfg.browser_state_file,
                user_agent=self.cfg.user_agent,
            )
        except LoginError as exc:
            raise AuthError(f"重新登录失败：{exc}") from exc
        self.state.set_token(token)
        self.state.save()
        self.client.token = token
        left = token_seconds_left(token)
        log.info(
            "登录成功：账号 %s，token 有效期 %s",
            token_holder(token),
            f"{left / 3600:.1f} 小时" if left else "未知",
        )

    def _fetch_items(self) -> list[dict]:
        """拉取通知；token 中途失效则重登一次再试。"""
        self.ensure_auth()
        try:
            return self.client.search_notifications(size=self.cfg.page_size,
                                                    channels=self.cfg.notify_channels)
        except AuthError as exc:
            log.warning("token 被服务端拒绝（%s），重新登录后重试", exc)
            self._relogin()
            return self.client.search_notifications(size=self.cfg.page_size,
                                                    channels=self.cfg.notify_channels)

    # ---------- 推送 ----------
    def _send(self, text: str) -> int:
        """广播给所有订阅者，返回成功送达的会话数；dry_run 时只记日志并返回 1。"""
        if self.dry_run:
            log.info("[dry-run] 本应推送：\n%s", text)
            return 1
        return self.broadcast(text)

    def broadcast(self, text: str) -> int:
        """把一条文本发给所有订阅者，返回成功送达的会话数。

        尽力而为语义：单个订阅者被拉黑（403）则移除；其他失败只记日志不重试。
        网络完全不可用（零送达）时抛 TelegramError，由调用方决定重试。
        """
        targets = self.state.subscriber_ids()
        if not targets:
            log.info("暂无订阅者，跳过推送")
            return 0
        sent = 0
        last_err: TelegramError | None = None
        for chat_id in targets:
            try:
                send_message(self.cfg.bot_token, chat_id, text, timeout=self.cfg.timeout_seconds)
                sent += 1
            except TelegramError as exc:
                if exc.blocked:
                    log.info("订阅者 %s 已拉黑/移除机器人，自动退订", chat_id)
                    self.state.remove_subscriber(chat_id)
                    self.state.save()
                else:
                    log.warning("推送给 %s 失败：%s", chat_id, exc)
                    last_err = exc
            time.sleep(0.05)  # 全局约 30 msg/s 限制，稍作节流
        if sent == 0 and last_err is not None:
            raise last_err
        return sent

    def flush_pending(self) -> int:
        """把待发队列广播出去。返回成功广播的通知条数。

        一条通知只要送达至少一个订阅者即记账；零订阅者或全部失败则保留队列，下轮重试。
        """
        pend = list(self.state.pending)
        if not pend:
            return 0
        if self.state.paused:
            log.info("已暂停推送：%d 条通知留在待发队列，/resume 后补发", len(pend))
            return 0
        if not self.cfg.bot_token and not self.dry_run:
            log.info("Telegram 未配置（缺 TELEGRAM_BOT_TOKEN），%d 条通知留在待发队列", len(pend))
            return 0
        if not self.dry_run and not self.state.subscriber_ids():
            log.info("暂无订阅者：%d 条通知留在待发队列，有人 /start 后自动补推", len(pend))
            return 0

        sent = 0
        # 补推前先说明一下，避免突然收到一串消息以为出 bug
        if len(pend) > 1:
            try:
                self._send(format_summary(len(pend)))
            except Exception as exc:  # noqa: BLE001
                log.warning("补推提示发送失败：%s", exc)

        for item in pend:
            text = format_notice(item, include_content=self.cfg.include_content)
            try:
                delivered = self._send(text)
            except TelegramError as exc:
                log.error("推送失败，保留在队列稍后重试：%s", exc)
                self.state.bump("errors")
                self.state.save()
                return sent
            if not delivered and not self.dry_run:
                # 零送达（如订阅者刚被拉黑并移除），保留待下轮重试
                log.warning("本轮零送达，%d 条通知留在队列稍后重试", len(pend) - sent)
                return sent
            self.state.drop_pending(item["id"])
            self.state.mark_pushed([item["id"]])
            self.state.bump("pushed")
            self.state.save()
            sent += 1
        return sent

    # ---------- 单轮 ----------
    def run_once(self, *, force_baseline: bool = False) -> dict:
        started = time.time()
        result = {"fetched": 0, "new": 0, "pushed": 0, "baseline": False, "pending": 0, "error": ""}
        try:
            items = self._fetch_items()
        except (AuthError, ApiError) as exc:
            result["error"] = str(exc)
            self.state.bump("errors")
            self.state.save()
            log.error("拉取通知失败：%s", exc)
            return result

        result["fetched"] = len(items)
        self.state.touch_check()

        # 首次运行（或强制）：只记录基线，不推送
        if force_baseline or not self.state.baseline_done:
            self.state.mark_pushed([it["id"] for it in items])
            self.state.data["baseline_done"] = True
            result["baseline"] = True
            log.info("建立基线：记录 %d 条现有通知，不推送", len(items))
            if (self.cfg.bot_token or self.dry_run):
                if self.cfg.notify_on_start and not self.state.data.get("started_notified"):
                    try:
                        self._send(format_start(len(items), self.cfg.interval_minutes))
                        self.state.data["started_notified"] = True
                    except Exception as exc:  # noqa: BLE001
                        log.warning("启动提示发送失败：%s", exc)
            self.state.save()
            return result

        # 找新增：既没推过，也不在待发队列里
        seen = self.state.seen_ids()
        queued = self.state.pending_ids()
        fresh = [it for it in items if it["id"] not in seen and it["id"] not in queued]
        # 接口按时间倒序，推送时按时间正序（先发生的先推）
        fresh.sort(key=lambda x: (x.get("time") or "", x["id"]))
        if fresh:
            log.info("发现 %d 条新通知：%s", len(fresh), " / ".join(i["title"][:30] for i in fresh))
            self.state.queue(fresh, self.cfg.max_pending)
        result["new"] = len(fresh)
        self.state.save()

        result["pushed"] = self.flush_pending()
        result["pending"] = len(self.state.pending)
        self.state.save()
        log.info(
            "本轮完成：抓取 %d 条 / 新增 %d 条 / 推送 %d 条 / 队列剩余 %d 条，用时 %.1fs",
            result["fetched"], result["new"], result["pushed"], result["pending"], time.time() - started,
        )
        return result

    def prune_pending(self, *, persist: bool = True) -> int:
        """丢掉待发队列里不属于 notify_channels 的条目（返回丢弃条数）。

        用于配置收紧后清理历史队列（例如从「混合流」改成只要校内通知）。
        """
        from .dekt import filter_channels

        pend = list(self.state.pending)
        if not pend:
            return 0
        keep = filter_channels(pend, self.cfg.notify_channels)
        dropped = len(pend) - len(keep)
        if dropped:
            self.state.data["pending"] = keep
            if persist:
                self.state.save()
        return dropped

    def _startup_checks(self) -> None:
        """启动自检：把「配置缺了什么」一次说清楚，别等到 401 才发现。"""
        if not (self.cfg.username and self.cfg.password):
            if self.client.token:
                log.warning(
                    "未配置 BUPT_USERNAME / BUPT_PASSWORD：当前 token 还能用，"
                    "但一旦过期（约 3 天）就无法自动续期，请在 .env 里补齐后重启"
                )
            else:
                log.error(
                    "既没有可用 token，也没有 BUPT_USERNAME / BUPT_PASSWORD —— "
                    "无法登录，监控会一直拿不到数据。请在 .env 里配置账号密码。"
                )
        if not self.cfg.bot_token:
            log.warning(
                "未配置 TELEGRAM_BOT_TOKEN：通知会进入待发队列（最多 %d 条）不会丢，配好后自动补推",
                self.cfg.max_pending,
            )
        elif not self.state.subscriber_ids() and not self.cfg.chat_id:
            log.warning("暂无订阅者：用户在 Telegram 里发 /start 即可订阅，订阅前通知排队不丢")
        left = token_seconds_left(self.client.token)
        if left is not None:
            log.info("当前 token 剩余有效期：%.1f 小时", left / 3600)
        if self.cfg.notify_channels:
            log.info("只推送这些频道：%s", "、".join(self.cfg.notify_channels))
        else:
            log.info("NOTIFY_CHANNELS 为空：不按频道过滤（接口返回的是混合流）")

    def run_once_locked(self, *, force_baseline: bool = False) -> dict:
        """带单实例锁跑一轮。供主循环和 Telegram /pull 命令共用。"""
        with single_instance(self.cfg.lock_file):
            return self.run_once(force_baseline=force_baseline)

    # ---------- 常驻 ----------
    def run_forever(self) -> None:
        interval = max(1, self.cfg.interval_minutes) * 60
        self._startup_checks()
        log.info("监控启动：每 %d 分钟检查一次", self.cfg.interval_minutes)
        bot = None
        if self.cfg.bot_token and not self.dry_run:
            from .bot import CommandBot
            bot = CommandBot(self.cfg, self.state, self)
            bot.register_commands()
            log.info("Telegram 命令已启用：/status /pause /resume /pull /latest /help")
        elif self.dry_run:
            log.info("dry-run 模式：不启用 Telegram 命令轮询")
        next_run = time.time()
        while True:
            try:
                now = time.time()
                if now >= next_run:
                    with single_instance(self.cfg.lock_file):
                        self.run_once()
                    next_run = time.time() + interval + random.uniform(0, 30)
                elif bot:
                    # 空闲时段长轮询命令；轮询时长不超过距下轮的剩余时间
                    bot.poll_and_handle(timeout=min(25, max(1, next_run - now)))
                else:
                    time.sleep(min(60, max(1, next_run - time.time())))
            except RuntimeError as exc:
                log.warning("跳过本轮：%s", exc)
                next_run = time.time() + 60
            except AuthError as exc:
                log.error("认证失败，下轮再试：%s", exc)
                next_run = time.time() + interval
            except Exception:  # noqa: BLE001
                log.exception("本轮异常，60 秒后重试")
                self.state.bump("errors")
                self.state.save()
                time.sleep(60 + random.uniform(0, 10))
                next_run = time.time() + interval


def fetch_latest(cfg: Config, limit: int = 10, *, token: str = "") -> list[dict]:
    """只读拉取（不碰状态文件），用于 --list。"""
    client = DektClient(cfg.api_base, token or cfg.token, timeout=cfg.timeout_seconds, user_agent=cfg.user_agent)
    return client.search_notifications(size=limit, channels=cfg.notify_channels)
