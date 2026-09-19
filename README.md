# bupt_notification

北邮第二课堂（dekt.bupt.edu.cn）校内通知监控，发现新通知自动推送到 Telegram。

配好账号后即可常驻运行：程序定期抓取通知列表，和已推送记录比对，一旦出现新通知就立即推送到所有订阅者的 Telegram。这是一个公开订阅的机器人：任何人给机器人发 `/start` 即可订阅，无需服务端任何配置；支持通过 Bot 命令远程查看与控制，适合部署在服务器或 NAS 上长期挂着。

## 功能特性

- **自动登录与续期**：登录接口需要 JS 生成的验证码，纯 HTTP 走不通，因此用 Playwright 驱动无头浏览器完成登录；拿到的 token（JWT，寿命约 3 天）存入状态文件，之后所有请求走轻量的纯 HTTP。token 剩余不足设定阈值时自动提前续期，中途失效也有 401 兜底重登。
- **不打扰式启动**：首次运行只建立基线（记录现有通知），不推送任何历史消息；之后只推真正的新通知。
- **消息不丢失**：推送失败、暂停期间、或 Telegram 尚未配置时，新通知进入待发队列（上限可配），恢复后自动补发；新订阅者加入时积压消息也会补发。
- **公开订阅 + 管理员命令**：任何人 `/start` 订阅、`/stop` 退订；控制型命令仅限管理员（`TELEGRAM_CHAT_ID`）。订阅者拉黑机器人时自动移除。
- **Telegram 命令机器人**：不出服务器也能远程查看状态、立即拉取、暂停/恢复推送。
- **单实例保护**：基于 `flock` 的锁，常驻进程与手动执行不会撞车。
- **零重依赖**：抓取与推送只用 Python 标准库；`playwright` 仅在登录环节使用。
- **开箱即用的容器化**：基于 Playwright 官方镜像（自带 Chromium 与系统依赖），带健康检查与日志轮转配置。

## 快速开始（Docker，推荐）

前置要求：已安装 Docker 与 docker compose。

### 1. 准备配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```ini
# 北邮第二课堂账号（用于首次登录及 token 过期后自动续期）
BUPT_USERNAME=你的学号
BUPT_PASSWORD=你的密码

# Telegram 推送
TELEGRAM_BOT_TOKEN=xxx      # 从 @BotFather 获取
TELEGRAM_CHAT_ID=xxx        # 见下方第 3 步
```

### 2. 启动容器

```bash
docker compose up -d        # 首次会构建镜像；也可先 docker compose pull 用预构建镜像
```

### 3. 订阅与验证

在 Telegram 里给你的机器人发一条 `/start`，即完成订阅。若想让控制型命令（/pause 等）可用，用下面命令检测你的 chat_id，填到 `.env` 的 `TELEGRAM_CHAT_ID`（管理员），然后 `docker compose restart`：

```bash
docker compose exec -T bupt-notification python -m bupt_notification detect-chat-id
```

验证：

```bash
docker compose logs -f                          # 观察日志：首轮建立基线，之后按周期检查
docker compose exec -T bupt-notification \
  python -m bupt_notification test-notify       # 发一条测试消息
```

收到测试消息即部署成功。之后把机器人分享给别人，对方发 `/start` 就能一起订阅。

## 本地运行（不用 Docker）

```bash
pip install -r requirements.txt   # 仅需 playwright
cp .env.example .env              # 填写配置，同上
python -m bupt_notification run   # 常驻运行
```

> 本地跑需要能找到 Chromium/Chrome（会自动探测常见路径，也可用 `CHROME_PATH` 指定）。

## 工作原理

```
┌─────────────┐   浏览器登录(仅首次/续期)   ┌──────────────┐
│  Playwright  │ ─────────────────────────▶ │ dekt.bupt.edu.cn │
└─────────────┘        获取 JWT token       └──────┬───────┘
       │                                          │ 纯 HTTP 拉取通知
       │ token 存入 state.json                    ▼
       │                                   去重比对（已推送/已入队）
       ▼                                          │
  提前续期（剩余 < 阈值）                          ▼
                                          新通知 → 待发队列 → 广播给全部订阅者 → Telegram
```

每轮检查的流程：拉取最新通知 → 与「已推送集合 + 待发队列」比对找出新增 → 入队 → 逐条广播给所有订阅者并记账。一条通知只要送达至少一个订阅者即视为完成；订阅者拉黑机器人会自动移除；零订阅者或全部失败时通知留在队列，下轮重试。任何环节失败都不会中断下一轮，错误会计入统计。

## Telegram 命令

常驻运行时，机器人会注册并响应以下命令。公开命令所有人可用；标「管理员」的仅限 `TELEGRAM_CHAT_ID` 对应的会话：

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/start` | 所有人 | 订阅推送（积压通知会立即补发） |
| `/stop` | 所有人 | 退订 |
| `/status` | 所有人 | 查看监控状态：运行/暂停、订阅者数、token 有效期、统计等 |
| `/latest [n]` | 所有人 | 列出最新 n 条通知（默认 10，最多 30） |
| `/help` | 所有人 | 查看全部命令 |
| `/pause` | 管理员 | 暂停推送（新通知照常入队，不丢） |
| `/resume` | 管理员 | 恢复推送并补发队列中的通知 |
| `/pull` | 管理员 | 立即执行一轮检查 |
| `/subscribers` | 管理员 | 查看订阅者列表 |

> 未配置 `TELEGRAM_CHAT_ID` 时，机器人照常运行和推送，但控制型命令不可用。

## CLI 子命令

`python -m bupt_notification <子命令>`（Docker 下用 `make <目标>`，见 Makefile）：

| 子命令 | 说明 |
| --- | --- |
| `run` | 常驻运行（默认）。`--dry-run` 只打印将要推送的内容 |
| `once` | 只跑一轮。`--baseline` 强制重建基线 |
| `list -n 10` | 只读列出最新通知，不改状态 |
| `login` | 强制浏览器登录刷新 token（`--headed` 显示浏览器窗口） |
| `detect-chat-id` | 从 getUpdates 自动检测 chat_id |
| `test-notify` | 发送一条测试消息验证推送链路 |
| `status` | 查看当前状态与统计 |
| `preview -n 3` | 本地打印推送排版预览，不需要 Telegram |
| `reset` | 重置状态，下次运行重新建基线 |
| `healthcheck` | 供 docker healthcheck 使用：超过 2 个周期未检查则报不健康 |

## 配置项

`TELEGRAM_BOT_TOKEN` 必填；`TELEGRAM_CHAT_ID` 为可选的管理员 ID（会自动成为首个订阅者，旧部署升级后原 chat_id 无缝保留）。其余配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BUPT_USERNAME` / `BUPT_PASSWORD` | 空 | 第二课堂账号；不填则必须有可用 token 且无法自动续期 |
| `BUPT_TOKEN` | 空 | 手动预置 token（一般留空，通常从浏览器 localStorage `secondclass.tokenv3` 获取） |
| `TELEGRAM_BOT_TOKEN` | 空 | Telegram Bot Token（必填） |
| `TELEGRAM_CHAT_ID` | 空 | 管理员 chat_id，启用控制型命令；订阅者动态注册无需配置 |
| `POLL_INTERVAL_MINUTES` | `30` | 检查周期（分钟） |
| `TOKEN_REFRESH_MARGIN_HOURS` | `6` | token 剩余不足该小时数时提前续期 |
| `PAGE_SIZE` | `50` | 每次拉取的通知条数 |
| `INCLUDE_CONTENT` | `false` | 推送是否附带正文全文 |
| `NOTIFY_ON_START` | `true` | 首次成功推送时发送「监控已启用」提示 |
| `MAX_PENDING` | `100` | 待发队列上限，超出丢弃最旧条目 |
| `HTTP_TIMEOUT_SECONDS` | `25` | HTTP 请求超时 |
| `STATE_FILE` | `./data/state.json` | 状态文件路径（compose 中注入 `/app/data/state.json`，挂载卷持久化） |
| `CHROME_PATH` | 自动探测 | 手动指定 Chromium/Chrome 可执行文件 |
| `LOG_FILE` | 无 | 额外写日志文件；容器下看 stdout 即可 |

## 数据与状态

`data/` 目录（挂载到容器 `/app/data`）保存：

- `state.json` — 已推送通知、待发队列、token、订阅者列表、统计等全部状态，容器重建不丢失
- `browser_state.json` — 浏览器登录上下文，加速后续自动登录
- `monitor.lock` — 单实例锁文件

删除 `state.json`（或执行 `reset`）后重新运行，会重新建立基线而不推送历史通知。

## 常见问题

**登录失败 / 一直拿不到 token？**
确认账号密码正确；可本地 `python -m bupt_notification login --headed` 观察浏览器行为排查。如果学校侧验证策略变更，可能需要更新 `web_login.py` 的登录流程。

**Telegram 一直收不到消息？**
依次检查：`.env` 中 bot token 是否填对 → 是否已发过 `/start` 完成订阅 → `test-notify` 是否能收到 → `/status` 命令是否有响应 → 容器日志有无推送报错。未订阅/未配置时通知会排队不丢。

**token 过期了怎么办？**
只要配置了账号密码，程序会在 token 临期前自动续期，无需干预。未配置账号密码的，补齐 `.env` 后重启即可。

## 许可

仅供个人学习与自用，请勿对本项目做压力测试或滥用学校接口。
