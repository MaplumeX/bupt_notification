"""配置加载：项目根目录 .env 优先，其次环境变量。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUE = {"1", "true", "yes", "on", "y"}


def _load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释、引号、空行。"""
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            data[key] = val
    return data


def _get(env: dict[str, str], key: str, default: str = "") -> str:
    """环境变量优先于 .env 文件。"""
    return os.environ.get(key, env.get(key, default)).strip()


def _get_int(env: dict[str, str], key: str, default: int) -> int:
    raw = _get(env, key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key, "")
    if not raw:
        return default
    return raw.lower() in _TRUE


def _get_channels(env: dict[str, str], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """解析频道白名单：NOTIFY_CHANNELS=校内通知,学术讲座

    - 键不存在 → 用默认值
    - 键存在但为空 → 空元组（表示不过滤，全都要）
    """
    if key not in os.environ and key not in env:
        return default
    raw = _get(env, key, "")
    if not raw:
        return ()
    parts = [p.strip() for p in raw.replace("，", ",").replace("、", ",").replace(" ", ",").split(",")]
    return tuple(p for p in parts if p)


@dataclass
class Config:
    # 站点
    api_base: str = "https://dekt.bupt.edu.cn"
    username: str = ""
    password: str = ""
    token: str = ""                 # 可从 .env 预置；通常由 state 文件维护

    # Telegram
    bot_token: str = ""
    chat_id: str = ""                # 管理员 chat_id（可选）：控制型命令仅对其生效；推送目标由订阅者动态决定

    # 轮询
    interval_minutes: int = 30
    refresh_margin_hours: int = 6  # token 剩余不足该值时提前续期（token 寿命 3 天）
    page_size: int = 50             # 每次拉取的最新通知条数
    notify_channels: tuple[str, ...] = ("校内通知",)  # 只要这些频道（接口返回的是混合流）
    include_content: bool = False   # 推送时是否附正文全文
    notify_on_start: bool = True    # 首次成功推送时告知"监控已启用"
    max_pending: int = 100          # 待发队列上限，超出的最旧条目丢弃
    timeout_seconds: int = 25

    # 路径 / 运行
    state_file: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "state.json")
    log_file: Path | None = None
    lock_file: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "monitor.lock")
    browser_state_file: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "browser_state.json")
    chrome_path: str = ""
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )

    @property
    def push_ready(self) -> bool:
        """Telegram 是否已配置好（有 bot token 即可推送，订阅者由 /start 动态注册）。"""
        return bool(self.bot_token)


def load_config(env_file: Path | None = None) -> Config:
    env = _load_env_file(env_file or (PROJECT_ROOT / ".env"))
    cfg = Config(
        api_base=_get(env, "BUPT_API_BASE", "https://dekt.bupt.edu.cn").rstrip("/"),
        username=_get(env, "BUPT_USERNAME"),
        password=_get(env, "BUPT_PASSWORD"),
        token=_get(env, "BUPT_TOKEN"),
        bot_token=_get(env, "TELEGRAM_BOT_TOKEN"),
        chat_id=_get(env, "TELEGRAM_CHAT_ID"),
        interval_minutes=_get_int(env, "POLL_INTERVAL_MINUTES", 30),
        refresh_margin_hours=_get_int(env, "TOKEN_REFRESH_MARGIN_HOURS", 6),
        page_size=_get_int(env, "PAGE_SIZE", 50),
        notify_channels=_get_channels(env, "NOTIFY_CHANNELS", ("校内通知",)),
        include_content=_get_bool(env, "INCLUDE_CONTENT", False),
        notify_on_start=_get_bool(env, "NOTIFY_ON_START", True),
        max_pending=_get_int(env, "MAX_PENDING", 100),
        timeout_seconds=_get_int(env, "HTTP_TIMEOUT_SECONDS", 25),
        log_file=(Path(_get(env, "LOG_FILE")) if _get(env, "LOG_FILE") else None),
        chrome_path=_get(env, "CHROME_PATH"),
    )
    if (raw := _get(env, "STATE_FILE")):
        cfg.state_file = Path(raw)
    return cfg
