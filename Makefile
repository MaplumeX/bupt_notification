# bupt_notification —— docker compose 快捷命令
# 用法：make help

SERVICE := bupt-notification
COMPOSE := docker compose
RUN := $(COMPOSE) exec -T $(SERVICE) python -m bupt_notification

.PHONY: help build up down restart ps logs health once list status preview login test-notify detect-chat-id reset prune shell

help:  ## 显示所有命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

build:  ## 构建镜像
	$(COMPOSE) build

up:  ## 构建并后台启动
	$(COMPOSE) up -d --build

down:  ## 停止并删除容器
	$(COMPOSE) down

restart:  ## 重启容器（改完 .env 后执行）
	$(COMPOSE) restart

ps:  ## 查看容器状态
	$(COMPOSE) ps

logs:  ## 跟踪日志
	$(COMPOSE) logs -f --tail=100

health:  ## 查看健康检查结果
	$(COMPOSE) exec -T $(SERVICE) python -m bupt_notification healthcheck

once:  ## 只跑一轮（首次会建立基线，不推送历史）
	$(RUN) once

list:  ## 列出最新通知（只读）
	$(RUN) list -n 10

status:  ## 查看当前状态
	$(RUN) status

preview:  ## 查看推送排版预览
	$(RUN) preview -n 3

login:  ## 浏览器登录并刷新 token
	$(RUN) login

test-notify:  ## 发一条 Telegram 测试消息
	$(RUN) test-notify

detect-chat-id:  ## 自动检测 Telegram chat_id
	$(RUN) detect-chat-id

reset:  ## 重置状态（下次重新建基线）
	$(RUN) reset

prune:  ## 丢弃待发队列里不属于 NOTIFY_CHANNELS 的条目
	$(RUN) prune

shell:  ## 进容器
	$(COMPOSE) exec $(SERVICE) bash
