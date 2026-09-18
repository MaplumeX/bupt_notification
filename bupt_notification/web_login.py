"""浏览器登录 —— 只在「没有 token」或「token 失效」时使用。

为什么必须用浏览器：登录接口 POST /api/v1/auth/sessions 需要 JS 生成的
验证码字段（code/captcha），纯 HTTP 提交会返回 420「验证码错误」。
拿到的 token 存在 localStorage['secondclass.tokenv3']，之后所有接口调用
都可以用纯 HTTP 完成。
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("bupt.login")

TOKEN_KEY = "secondclass.tokenv3"


class LoginError(RuntimeError):
    pass


def find_chrome(explicit: str = "") -> str:
    """定位本地 Chromium/Chrome 可执行文件。"""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("CHROME_PATH"):
        candidates.append(os.environ["CHROME_PATH"])
    home = Path.home()
    candidates += sorted(glob.glob(str(home / ".agent-browser/browsers/*/chrome")), reverse=True)
    candidates += sorted(glob.glob(str(home / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome")), reverse=True)
    candidates += sorted(glob.glob(str(home / ".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")), reverse=True)
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if (found := shutil.which(name)):
            candidates.append(found)
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise LoginError(
        "找不到可用的 Chrome/Chromium。请设置 CHROME_PATH，或执行：\n"
        "  npm install -g agent-browser && agent-browser install"
    )


def browser_login(
    api_base: str,
    username: str,
    password: str,
    *,
    chrome_path: str = "",
    browser_state_file: str | Path | None = None,
    user_agent: str = "",
    headless: bool = True,
    timeout_ms: int = 60_000,
) -> str:
    """登录并返回 token。成功后会保存浏览器会话（下次可复用）。"""
    if not username or not password:
        raise LoginError("缺少 BUPT_USERNAME / BUPT_PASSWORD，无法登录")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise LoginError(
            "未安装 playwright，无法自动登录。请执行：pip install playwright"
        ) from exc

    exe = find_chrome(chrome_path)
    state_path = Path(browser_state_file) if browser_state_file else None
    log.info("浏览器登录中（%s）…", exe)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=exe,
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx_kwargs: dict = {"viewport": {"width": 1366, "height": 900}, "locale": "zh-CN"}
            if user_agent:
                ctx_kwargs["user_agent"] = user_agent
            if state_path and state_path.is_file():
                ctx_kwargs["storage_state"] = str(state_path)
            ctx = browser.new_context(**ctx_kwargs)
            page = ctx.new_page()

            # 换个 UA 降低被识别为 headless 的概率
            if user_agent:
                page.set_extra_http_headers({"Accept-Language": "zh-CN,zh;q=0.9"})

            page.goto(f"{api_base}/login", wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_selector("#username", timeout=30_000)
            except Exception as exc:
                raise LoginError(f"登录页没出现账号输入框（{exc}）") from exc

            page.fill("#username", username)
            page.fill("#password", password)
            page.locator("button:has-text('登录')").first.click()

            token = ""
            deadline_ms = 45_000
            waited = 0
            while waited < deadline_ms:
                page.wait_for_timeout(1000)
                waited += 1000
                try:
                    token = page.evaluate(f"localStorage['{TOKEN_KEY}'] || ''") or ""
                except Exception:
                    token = ""
                if token and len(token) > 40:
                    break

            if not token or len(token) <= 40:
                body = ""
                try:
                    body = (page.inner_text("body") or "")[:300].replace("\n", " ")
                except Exception:
                    pass
                raise LoginError(
                    "登录失败：没拿到 token。页面提示：" + (body or "(空)") +
                    " —— 常见原因：账号密码错误、需要验证码、账号被锁、网络异常。"
                )

            if state_path:
                try:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    ctx.storage_state(path=str(state_path))
                except Exception as exc:
                    log.warning("保存浏览器会话失败（不影响使用）：%s", exc)

            log.info("登录成功，已获取 token（%d 字符）", len(token))
            return token
        finally:
            browser.close()
