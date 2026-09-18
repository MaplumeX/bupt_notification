# bupt_notification

北邮「第二课堂」**校内通知监控** → **Telegram 机器人推送**。
每 30 分钟检查一次，发现新通知立刻推送到你的 Telegram。

## 它是怎么工作的

```
                    ┌──────────────────────────────────────┐
                    │  dekt.bupt.edu.cn（第二课堂）          │
                    │  POST /api/v1/news/search            │
                    │  body: {"type":"notification", ...}   │
                    └───────────────┬──────────────────────┘
                                    │ HTTP + Authorization: Bearer <token>
                                    ▼
   ┌────────────────┐    ┌────────────────────┐    ┌──────────────────┐
   │ 每 30 分钟轮询   │───▶│ 与 state.json 比对   │───▶│ 新条目入待发队列    │
   │ monitor.py     │    │ 去重 / 建基线        │    │ pending[]        │
   └────────────────┘    └────────────────────┘    └────────┬─────────┘
                                                            │ 逐条发送
                                    ┌───────────────────────▼─────────┐
                                    │ Telegram Bot API sendMessage    │
                                    └─────────────────────────────────┘
```

实测得到的两条关键结论：

1. **抓取完全不需要浏览器。** 接口用 `Authorization: Bearer <token>` 认证，
   token 就是浏览器 `localStorage['secondclass.tokenv3']` 里那串，
   纯 HTTP（本项目的热路径只用 Python 标准库）即可稳定抓取。
2. **只有登录/换 token 需要浏览器。** `POST /api/v1/auth/sessions` 要求 JS 生成的
   验证码字段，纯 HTTP 提交会返回 420「验证码错误」。所以 token 失效时才会启动
   一次无头 Chromium 重新登录（约 10 秒），拿到新 token 后继续走 HTTP。

## 目录结构

```
bupt_notification/
├── bupt_notification/
│   ├── config.py        # .env / 环境变量配置
│   ├── dekt.py          # 第二课堂 API 客户端（标准库 HTTP）
│   ├── web_login.py     # 浏览器登录（仅换 token 时用，playwright）
│   ├── state.py         # 基线 / 已推送 / 待发队列（原子写 JSON）
│   ├── telegram.py      # Bot API 推送 + 消息排版
│   ├── monitor.py       # 轮询主循环：抓取 → 去重 → 入队 → 推送
│   └── cli.py           # 命令行
├── data/                # 运行期状态（state.json 含 token，权限 600）
├── deploy/bupt-notification.service
├── install.sh
└── .env                 # 账号 + bot token（权限 600，不要提交到 git）
```

## 安装

```bash
cd /root/bupt_notification
./install.sh          # 建 .venv、装 playwright、写 systemd 服务（不会自动启动）
```

## 配置

编辑 `.env`：

```ini
BUPT_USERNAME=2024211295      # 学工号
BUPT_PASSWORD=******          # 密码

TELEGRAM_BOT_TOKEN=           # @BotFather 创建机器人后拿到的 token
TELEGRAM_CHAT_ID=             # 你的 chat id，见下

POLL_INTERVAL_MINUTES=30      # 检查频率
INCLUDE_CONTENT=false         # true = 推送时附上正文全文
```

### 创建机器人并拿到 chat_id

1. Telegram 里找 **@BotFather** → `/newbot` → 起名字 → 得到 `123456789:AAE...`
2. 把 token 填进 `.env` 的 `TELEGRAM_BOT_TOKEN`
3. 给刚建的机器人发一条 `/start`
4. 自动拿 chat_id：

```bash
./.venv/bin/python -m bupt_notification detect-chat-id
# 输出 TELEGRAM_CHAT_ID=xxxxxxxx，填进 .env
```

## 常用命令

```bash
PY=./.venv/bin/python

$PY -m bupt_notification login          # 浏览器登录并保存 token
$PY -m bupt_notification once           # 只跑一轮（首次会建立基线，不推送历史）
$PY -m bupt_notification once --dry-run # 只打印将要推送的内容，不真发
$PY -m bupt_notification list -n 10     # 看最新通知（只读，不改状态）
$PY -m bupt_notification preview -n 3   # 看推送排版
$PY -m bupt_notification test-notify    # 发一条测试消息
$PY -m bupt_notification status         # 当前状态
$PY -m bupt_notification reset          # 重置（下次重新建基线）
$PY -m bupt_notification run            # 常驻（systemd 用这个）
```

## 常驻运行

```bash
sudo systemctl enable --now bupt-notification
journalctl -u bupt-notification -f        # 看日志
sudo systemctl restart bupt-notification
```

## 行为说明（重要）

- **首次运行只建立基线，不推送。** 现有通知会被记下来，之后只推*新增*的。
- **待发队列不丢消息。** 如果 Telegram 没配好、网络抖动、token 失效，抓到的通知会留在
  `pending` 队列里，下一轮重试；补推超过 1 条时会先发一条「本周期补推 N 条」的说明。
- **去重键是 `news_id`**，同一通知不会重复推。已推送 id 保留最近 2000 条。
- **token 自动续期。** 接口返回 401 时自动走一次浏览器登录，无需人工干预。
  想手动换：`python -m bupt_notification login`。
- **定时任务有锁。** `data/monitor.lock` 用 flock 防止 cron 与手动执行撞车。
- 站点前置了瑞数类 WAF；带 Bearer token 的普通 HTTP 请求不受影响（已实测）。

## 排障

| 现象 | 处理 |
|---|---|
| `找不到可用的 Chrome/Chromium` | 设 `CHROME_PATH`，或 `npm install -g agent-browser && agent-browser install` |
| `登录失败：没拿到 token` | 密码改了 / 需要验证码 / 账号被锁；用 `login --headed` 开有头浏览器看页面 |
| `推送失败，保留在队列稍后重试：Unauthorized` | bot token 不对或 chat_id 不对；先跑 `test-notify` |
| 想重新把当前通知当基线 | `python -m bupt_notification reset` 然后跑 `once` |
| 想改频率 | `.env` 里 `POLL_INTERVAL_MINUTES` 改完 `systemctl restart bupt-notification` |
