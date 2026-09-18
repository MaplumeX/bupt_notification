#!/usr/bin/env bash
# 一键安装：建 venv、装依赖、写入 systemd 服务（不自动启动）。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "==> 项目目录：$PROJECT_DIR"

if ! command -v python3 >/dev/null; then
  echo "缺少 python3" >&2
  exit 1
fi

echo "==> 创建虚拟环境 .venv"
python3 -m venv .venv 2>/dev/null || uv venv .venv --python 3.11
./.venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true

echo "==> 安装 playwright（仅用于登录刷新 token）"
if command -v uv >/dev/null; then
  uv pip install --python ./.venv/bin/python -r requirements.txt
else
  ./.venv/bin/python -m pip install -q --upgrade pip
  ./.venv/bin/python -m pip install -q -r requirements.txt
fi

echo "==> 初始化 data 目录"
mkdir -p data
chmod 700 data 2>/dev/null || true
[ -f .env ] && chmod 600 .env || true

echo "==> 写 systemd 服务 /etc/systemd/system/bupt-notification.service"
SUDO=""
if [ "$(id -u)" != "0" ]; then
  SUDO="sudo"
fi
$SUDO tee /etc/systemd/system/bupt-notification.service >/dev/null <<UNIT
[Unit]
Description=BUPT 第二课堂校内通知监控（Telegram 推送）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python -m bupt_notification run
Restart=always
RestartSec=30
# 日志进 journal：journalctl -u bupt-notification -f
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
echo
echo "安装完成。接下来："
echo "  1) 编辑 .env 填好 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
echo "  2) ./.venv/bin/python -m bupt_notification login        # 登录拿 token"
echo "  3) ./.venv/bin/python -m bupt_notification test-notify  # 测试推送"
echo "  4) sudo systemctl enable --now bupt-notification        # 启动监控"
echo "  5) journalctl -u bupt-notification -f                   # 看日志"
