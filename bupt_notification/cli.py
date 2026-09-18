"""命令行入口。

常用：
  python -m bupt_notification once          # 跑一轮（建基线 / 推送新增）
  python -m bupt_notification run           # 常驻，每 30 分钟一轮
  python -m bupt_notification list -n 10    # 只看最新通知，不改状态
  python -m bupt_notification login         # 强制浏览器登录刷新 token
  python -m bupt_notification detect-chat-id
  python -m bupt_notification test-notify
  python -m bupt_notification status
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from . import __version__
from .config import PROJECT_ROOT, Config, load_config
from .dekt import ApiError, AuthError, DektClient
from .monitor import Monitor, fetch_latest, single_instance
from .state import State
from .telegram import TelegramError, detect_chat_id, format_notice, get_me, send_message
from .web_login import browser_login


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if cfg.log_file:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(cfg.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    Monitor(cfg, dry_run=args.dry_run).run_forever()
    return 0


def cmd_once(cfg: Config, args: argparse.Namespace) -> int:
    state = State(cfg.state_file)
    mon = Monitor(cfg, state, dry_run=args.dry_run)
    with single_instance(cfg.lock_file):
        res = mon.run_once(force_baseline=args.baseline)
    print(
        f"抓取 {res['fetched']} 条 | 新增 {res['new']} 条 | 推送 {res['pushed']} 条 | "
        f"队列剩余 {res['pending']} 条" + (" | 本轮建立基线" if res["baseline"] else "")
    )
    if res["error"]:
        print(f"错误：{res['error']}")
        return 1
    return 0


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    token = State(cfg.state_file).token or cfg.token
    try:
        items = fetch_latest(cfg, limit=args.limit, token=token)
    except AuthError:
        print("本地 token 无效，尝试浏览器登录…")
        token = browser_login(cfg.api_base, cfg.username, cfg.password, chrome_path=cfg.chrome_path,
                              browser_state_file=cfg.browser_state_file, user_agent=cfg.user_agent)
        State(cfg.state_file).set_token(token)
        items = fetch_latest(cfg, limit=args.limit, token=token)
    except ApiError as exc:
        print(f"拉取失败：{exc}")
        return 1

    for i, it in enumerate(items, 1):
        print(f"{i:>2}. [{it['time_local']}] {it['author']} — {it['title']}")
        print(f"    {it['url']}")
    print(f"\n共 {len(items)} 条（最新在前）")
    return 0


def cmd_login(cfg: Config, args: argparse.Namespace) -> int:
    token = browser_login(
        cfg.api_base, cfg.username, cfg.password,
        chrome_path=cfg.chrome_path, browser_state_file=cfg.browser_state_file,
        user_agent=cfg.user_agent, headless=not args.headed,
    )
    state = State(cfg.state_file)
    state.set_token(token)
    state.save()
    # 验证 token 可用
    client = DektClient(cfg.api_base, token, timeout=cfg.timeout_seconds, user_agent=cfg.user_agent)
    total = client.notification_total()
    print(f"登录成功，token 已保存（{len(token)} 字符）。通知总数：{total}")
    return 0


def cmd_detect_chat_id(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.bot_token:
        print("请先在 .env 里填 TELEGRAM_BOT_TOKEN（从 @BotFather 拿）")
        return 1
    me = get_me(cfg.bot_token)
    print(f"机器人：@{me.get('username')}（{me.get('first_name')}）")
    chat_id = detect_chat_id(cfg.bot_token)
    if not chat_id:
        print("没找到 chat_id。请先在 Telegram 里给这个机器人发一条消息（如 /start），再重新运行。")
        return 1
    print(f"检测到 chat_id：{chat_id}")
    print(f"把它写进 .env：TELEGRAM_CHAT_ID={chat_id}")
    return 0


def cmd_test_notify(cfg: Config, args: argparse.Namespace) -> int:
    if not cfg.push_ready:
        print("Telegram 未配置：需要 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
        return 1
    try:
        send_message(cfg.bot_token, cfg.chat_id,
                     "🧪 <b>测试消息</b>\nbupt_notification 推送链路正常。", timeout=cfg.timeout_seconds)
    except TelegramError as exc:
        print(f"发送失败：{exc}")
        return 1
    print("测试消息已发送，请查看 Telegram。")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    st = State(cfg.state_file)
    d = st.data
    age = ""
    if d.get("token_updated_at"):
        age = f"{int(time.time() - d['token_updated_at']) // 3600} 小时前"
    print(f"版本        : {__version__}")
    print(f"状态文件    : {cfg.state_file}")
    print(f"基线已建立  : {'是' if st.baseline_done else '否'}")
    print(f"已记录通知  : {len(d.get('seen', []))} 条")
    print(f"待发队列    : {len(st.pending)} 条")
    print(f"token       : {'有' if st.token else '无'}（更新于 {age or '未知'}）")
    print(f"上次检查    : {d.get('last_check_iso') or '还没跑过'}")
    print(f"统计        : {d.get('stats')}")
    print(f"Telegram    : {'已配置' if cfg.push_ready else '未配置（缺 token/chat_id，通知会排队不丢）'}")
    print(f"检查频率    : 每 {cfg.interval_minutes} 分钟")
    return 0


def cmd_reset(cfg: Config, args: argparse.Namespace) -> int:
    st = State(cfg.state_file)
    st.reset()
    print("状态已重置：下次运行会重新建立基线（现有通知不推送）。")
    return 0


def cmd_preview_format(cfg: Config, args: argparse.Namespace) -> int:
    """把推送内容直接打印出来，用来检查排版，不需要 Telegram。"""
    token = State(cfg.state_file).token or cfg.token
    items = fetch_latest(cfg, limit=args.limit, token=token)
    for it in items[: args.limit]:
        print("-" * 60)
        print(format_notice(it, include_content=cfg.include_content))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bupt_notification", description="北邮第二课堂校内通知监控 → Telegram")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("run", help="常驻运行（每 N 分钟一轮）")
    sp.add_argument("--dry-run", action="store_true", help="只打印将要推送的内容，不真的发送")

    sp = sub.add_parser("once", help="只跑一轮")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--baseline", action="store_true", help="强制把当前通知记为基线，不推送")

    sp = sub.add_parser("list", help="列出最新通知（只读，不改状态）")
    sp.add_argument("-n", "--limit", type=int, default=10)

    sp = sub.add_parser("login", help="浏览器登录并刷新 token")
    sp.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")

    sub.add_parser("detect-chat-id", help="从 getUpdates 自动找出 chat_id")
    sub.add_parser("test-notify", help="发一条测试消息")
    sub.add_parser("status", help="查看当前状态")
    sub.add_parser("reset", help="重置状态（重新建立基线）")

    sp = sub.add_parser("preview", help="打印推送排版预览（不发送）")
    sp.add_argument("-n", "--limit", type=int, default=3)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    setup_logging(cfg, getattr(args, "verbose", False))
    cmd = args.cmd or "run"

    handlers = {
        "run": cmd_run, "once": cmd_once, "list": cmd_list, "login": cmd_login,
        "detect-chat-id": cmd_detect_chat_id, "test-notify": cmd_test_notify,
        "status": cmd_status, "reset": cmd_reset, "preview": cmd_preview_format,
    }
    handler = handlers[cmd]
    try:
        return handler(cfg, args)
    except AuthError as exc:
        print(f"认证失败：{exc}")
        return 1
    except ApiError as exc:
        print(f"接口错误：{exc}")
        return 1
    except TelegramError as exc:
        print(f"Telegram 错误：{exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
