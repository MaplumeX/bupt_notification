# 基于 playwright 官方镜像：自带 Chromium + 系统依赖，
# 免去在容器里 playwright install --with-deps（省几百 MB 下载和一堆系统包问题）。
# 版本与 requirements.txt 的 playwright 保持一致。
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    STATE_FILE=/app/data/state.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bupt_notification ./bupt_notification
COPY tests ./tests

RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 常驻监控：每 POLL_INTERVAL_MINUTES 分钟一轮
CMD ["python", "-m", "bupt_notification", "run"]
