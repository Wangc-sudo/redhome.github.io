# 杭州销售完成率榜单 - 后端服务镜像
# 纯 Python 标准库实现，使用 slim 基础镜像保持最小体积
FROM python:3.12-slim

LABEL org.opencontainers.image.title="leaderboard-server"
LABEL org.opencontainers.image.description="杭州销售完成率榜单 API + 前端"

# 容器内以非 root 运行
RUN useradd -r -m -u 1000 app

WORKDIR /app
COPY server.py /app/server.py
COPY static/ /app/static/

# SQLite 数据目录（挂 volume 持久化）
RUN mkdir -p /app/data && chown -R app:app /app
VOLUME ["/app/data"]

USER app
ENV LEADERBOARD_DB=/app/data/leaderboard.db

EXPOSE 8300
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8300/healthz',timeout=3)"

CMD ["python", "server.py"]
