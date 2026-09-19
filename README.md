# bupt_notification

北邮「第二课堂」**校内通知监控** → **Telegram 机器人推送**。
每 30 分钟检查一次，发现新通知立刻推送到你的 Telegram。**Docker Compose 部署。**

## 它是怎么工作的

```
                        ┌────────────────────────────────────────┐
                        │  dekt.bupt.edu.cn（第二课堂）            │
                        │  POST /api/v1/news/search               │
                        │  body: {"type":"notification", ...}     │
                        └───────────────┬────────────────────────┘
                                        │ HTTP + Authorization: Bearer <token>
                                        ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │ docker 容器 bupt-notification（playwright 官方镜像 + 本项目代码）        │
 │                                                                      │
 │  ┌──────────────┐   ┌────────────────┐   ┌──────────────────┐        │
 │  │ 每 30 分钟轮询 │──▶│ 与 state.json   │──▶│ 新条目进待发队列    │        │
 │  │ monitor.py   │   │ 去重 / 建基线    │   │ pending[]        │        │
 │  └──────────────┘   └────────────────┘   └────────┬─────────┘        │
 │         ▲ token 失效时自动重登                      │ 逐条发送           │
 │         └── 无头 Chromium（镜像自带）               ▼                  │
 │                                    ┌────────────────────────┐        │
 │                                    │ Telegram Bot API        │        │
 │                                    └────────────────────────┘        │
 └──────────────────────────────┬───────────────────────────────────────┘
                                │ 挂载卷
                       ┌────────▼────────┐
                       │ ./data          │  state.json（基线/已推/待发/token）
                       │                 │  browser_state.json（会话）
                       └─────────────────┘
```

实测得到的两条关键结论：

1. **抓取完全不需要浏览器。** 接口用 `Authorization: Bearer <token>` 认证，token 就是浏览器
   `localStorage['secondclass.tokenv3']` 里那串；纯 HTTP（热路径只用 Python 标准库）即可稳定抓取。
2. **只有登录/换 token 需要浏览器。** `POST /api/v1/auth/sessions` 要求 JS 生成的验证码字段，
   纯 HTTP 提交会返回 420「验证码错误」。所以 token 失效时才启动一次容器内的无头 Chromium
   重新登录（约 15 秒），拿到新 token 后继续走 HTTP。

## 目录结构

```
bupt_notification/
├── bupt_notification/          # 应用代码（纯标准库跑热路径）
│   ├── config.py               # .env / 环境变量配置
│   ├── dekt.py                 # 第二课堂 API 客户端
│   ├── web_login.py            # 浏览器登录（仅换 token，playwright）
│   ├── state.py                # 基线 / 已推送 / 待发队列（原子写 JSON）
│   ├── telegram.py             # Bot API 推送 + 命令轮询 + 消息排版
│   ├── bot.py                 # Telegram 命令处理（/status /pause /pull /latest…）
│   ├── monitor.py              # 轮询主循环
│   └── cli.py                  # 命令行
├── Dockerfile                  # 基于 mcr.microsoft.com/playwright/python（自带 Chromium）
├── docker-compose.yml          # 服务定义 + 健康检查 + 日志上限
├── Makefile                    # 常用命令快捷方式
├── .env                        # 账号 + bot token（600 权限，已被 .dockerignore 排除）
├── data/                       # 挂载卷：运行状态（含 token，600 权限）
└── README.md
```

## 快速开始

```bash
cd /root/bupt_notification

# 1) 从模板生成本机配置并填写（账号密码、bot token、chat_id）
cp .env.example .env && vi .env

# 2) 构建并启动
docker compose up -d --build

# 3) 看一眼日志
docker compose logs -f
```

首次运行会**记录基线、不推送历史通知**；之后只推新增。

## 配置（.env）

**没有任何凭据写在代码里。** 一切通过 `.env`（本机）/ 环境变量注入：

```bash
cp .env.example .env    # 模板在 .env.example，只有占位符
vi .env                 # 填你自己的账号、bot token、chat_id
```

```ini
BUPT_USERNAME=你的学工号           # 用于登录 / token 到期自动续期
BUPT_PASSWORD=你的密码
TELEGRAM_BOT_TOKEN=你的bot token
TELEGRAM_CHAT_ID=你的chat id

POLL_INTERVAL_MINUTES=30          # 检查频率
TOKEN_REFRESH_MARGIN_HOURS=6      # token 剩余不足 N 小时就提前续期
INCLUDE_CONTENT=false             # true = 推送时附正文全文
MAX_PENDING=100                   # 待发队列上限
```

- `.env` 在 `.gitignore` 和 `.dockerignore` 里，**既不会进 git，也不会进镜像层**
- 运行期 token 存在 `./data/state.json`（600 权限，同样不入库），日志只打印账号和剩余有效期，**从不打印 token**
- 改完 `.env`：`docker compose restart`

### 创建机器人并拿到 chat_id

1. Telegram 找 **@BotFather** → `/newbot` → 得到 `123456789:AAE...`，填进 `.env`
2. 给新机器人发一条 `/start`
3. 自动检测 chat_id：

```bash
make detect-chat-id          # 或 docker compose exec -T bupt-notification \
                             #    python -m bupt_notification detect-chat-id
```
把输出的 `TELEGRAM_CHAT_ID=...` 填进 `.env`，然后：

```bash
make test-notify && docker compose restart
```

## 机器人命令（Telegram 里直接发）

监控常驻运行时，在等待下一轮检查的间隙会长轮询 Telegram 命令（只响应 `.env` 里配置的那个 chat_id）：

| 命令 | 说明 |
| --- | --- |
| `/status` | 查看监控状态（暂停/运行、token 有效期、队列、统计等） |
| `/pause` | 暂停推送：通知照常抓取并进入待发队列，不会丢 |
| `/resume` | 恢复推送，并立即补发队列里的通知 |
| `/pull` | 不等下一个周期，立即拉取检查一轮（可代替等定时） |
| `/latest` | 列出最新 10 条通知（可带参数 `/latest 20`，上限 30 条） |
| `/help` | 查看全部命令 |

## 常用命令（Makefile）

```bash
make help          # 列出全部命令
make up            # 构建 + 后台启动
make logs          # 跟踪日志
make ps            # 容器状态（含健康状态）
make health        # 健康检查结果
make once          # 只跑一轮（首次 = 建基线）
make list          # 看最新 10 条通知（只读）
make preview       # 看推送排版
make status        # 当前状态（基线/队列/token 年龄）
make login         # 浏览器登录刷新 token
make test-notify   # 发一条 Telegram 测试消息
make reset         # 重置状态（下次重新建基线）
make shell         # 进容器
make restart       # 改完 .env 后重启
```

不用 Makefile 也可以，等价写法：

```bash
docker compose exec -T bupt-notification python -m bupt_notification status
docker compose run --rm bupt-notification python -m bupt_notification list -n 10
```

## 运维要点

- **数据在 `./data` 卷里**：`state.json`（已推 id、待发队列、token）、`browser_state.json`。
  容器重建/升级都不会丢。备份：`tar czf backup.tgz data/`。
- **健康检查**：容器内每 5 分钟跑一次 `healthcheck` 子命令，若超过 2 个轮询周期没成功检查则
  标记 unhealthy（`docker compose ps` 可见）。
- **日志**：`docker compose logs`；已限制 json-file 单文件 10MB × 3。
- **重启策略**：`unless-stopped`，宿主重启后自动拉起。
- **改代码后**：`docker compose up -d --build`。
- **镜像体积**：playwright 官方镜像自带 Chromium + 系统依赖（约 2GB），换来的是
  换 token 时的浏览器登录开箱可用，不用自己装 Chrome 和一堆 .so。

## 认证与 token 续期

登录/续期**全部自动**，不需要手工贴 token：

| 时机 | 行为 |
|---|---|
| 启动时没有 token | 用 `.env` 的账号密码跑一次容器内无头 Chromium 登录，token 存进 `data/state.json` |
| 启动自检 | 打印当前 token 剩余有效期；缺账号密码/缺 bot 配置会明确告警（不会静默失败） |
| 每轮轮询前 | 解析 JWT 的 `exp`，**剩余不足 `TOKEN_REFRESH_MARGIN_HOURS`（默认 6 小时）就提前续期** |
| 接口返回 401/403 | 立即重登一次再重试本轮请求（兜底，防止 token 被服务端提前作废） |
| 未配置账号密码时 | 有 token 仍可运行，但会告警"到期后无法自动续期"；没有 token 则明确报错 |

> 实测：该 token 是 JWT，`exp - iat = 259200s`，**寿命正好 3 天**，所以提前续期是必需的，
> 否则每 3 天就会出现一段抓不到数据的时间窗。
> 想手动立刻换：`make login`（会重新登录并把新 token 写进 `data/state.json`）。

## 行为说明（重要）

- **首次运行只建立基线，不推送。** 现有通知会被记下来，之后只推*新增*的。
- **待发队列不丢消息。** Telegram 没配好、网络抖动、token 失效时，抓到的通知留在 `pending`
  队列，下一轮重试；补推超过 1 条会先发一条「本周期补推 N 条」说明。
- **去重键是 `news_id`**，同一通知不会重复推；已推送 id 保留最近 2000 条。
- **token 自动续期**：接口 401 时自动走一次容器内浏览器登录，无需人工干预；
  手动换：`make login`。
- **定时任务有锁**：`data/monitor.lock` 用 flock 防止手动执行与常驻轮询撞车
  （撞车时 `make once` 会提示"已有另一个实例在运行"，等下一轮即可）。
- 站点前置瑞数类 WAF；带 Bearer token 的普通 HTTP 请求不受影响（已实测）。

## 排障

| 现象 | 处理 |
|---|---|
| `docker compose ps` 显示 unhealthy | `make logs` 看原因；多半是 token 失效后登录失败（密码改了/需要验证码） |
| `登录失败：没拿到 token` | 用 `docker compose run --rm bupt-notification python -m bupt_notification login --headed` 看页面（需要 X11，一般直接看日志里的页面提示就够） |
| `推送失败，保留在队列稍后重试：Unauthorized` | bot token 或 chat_id 不对，先 `make test-notify` |
| 想重新把当前通知当基线 | `make reset` 然后 `make once` |
| 想改频率 | `.env` 里改 `POLL_INTERVAL_MINUTES`，`make restart` |
| 容器内时间不对 | compose 里已设 `TZ=Asia/Shanghai`；日志时间应为北京时间 |
| 数据目录权限 | 容器以 root 运行，`data/` 归 root 即可；换非 root 需自行 `chown` |

## 不用 Docker 也能跑（可选）

代码热路径零第三方依赖，只有换 token 才需要 playwright：

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m bupt_notification once
```
