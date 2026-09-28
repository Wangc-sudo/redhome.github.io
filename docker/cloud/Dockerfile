# dops-app 生产运行时镜像（全量迁云）。
# 只装运行时（python3 + requirements），代码经 /opt/dops/repo bind-mount 进容器
# （见 docker-compose.dops.yml），故镜像不含业务代码、不随日常迭代重建；
# 仅 requirements.txt 变更时由 ops/deploy.sh 触发重建。
# 之前 dops-app:latest 是服务器上手工构建、无版本化 Dockerfile——本文件补上这个缺口，
# 让镜像构建可评审、可复现、可进 CI。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        python3 python3-pip ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
# PIP_INDEX_URL：国内构建可传镜像源（如 https://mirrors.aliyun.com/pypi/simple/）
ARG PIP_INDEX_URL=""
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      python3 -m pip install --break-system-packages --no-cache-dir \
        -i "$PIP_INDEX_URL" -r /tmp/requirements.txt; \
    else \
      python3 -m pip install --break-system-packages --no-cache-dir \
        -r /tmp/requirements.txt; \
    fi

WORKDIR /opt/dops/repo
# 无 ENTRYPOINT/CMD：各服务在 compose 里指定各自的 python3 -m ... 入口。
