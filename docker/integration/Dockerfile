FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# apt 源固定国内镜像（可用 --build-arg APT_MIRROR 覆盖）
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do \
      if [ -f "$f" ]; then \
        sed -i "s|http://archive.ubuntu.com|http://${APT_MIRROR}|g; s|http://security.ubuntu.com|http://${APT_MIRROR}|g" "$f"; \
      fi; \
    done; \
    apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
# 默认值固定为国内源 + BuildKit pip 缓存挂载（显式传空串仍回退官方源）。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$PIP_INDEX_URL" ]; then \
      python3 -m pip install --break-system-packages \
        -i "$PIP_INDEX_URL" -r /tmp/requirements.txt; \
    else \
      python3 -m pip install --break-system-packages \
        -r /tmp/requirements.txt; \
    fi

COPY . /app
RUN chmod 0755 /app/docker/integration/run-tests.sh

ENTRYPOINT ["/app/docker/integration/run-tests.sh"]
