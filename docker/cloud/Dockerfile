# dops-app 生产运行时镜像（全量迁云）。
# 只装运行时（python 基座 + requirements），代码经 /opt/dops/repo bind-mount 进容器
# （见 docker-compose.dops.yml），故镜像不含业务代码、不随日常迭代重建；
# 仅 requirements.txt / 本文件变更时由 ops/deploy.sh 触发重建。
#
# 2026-11-18 起基础镜像为公共基座 dops-base-python312（gitee dops-docker 仓）：
#   * OS 包与公共依赖（PyMySQL / PyYAML / cryptography / nacos-sdk / croniter /
#     openpyxl）在基座里预热，本文件只装增量；
#   * 不再用 apt 装 python——CN 网络下 apt 是构建最长耗时（ubuntu noble 的
#     universe 索引 19MB，实测可卡住数分钟）；
#   * 基座只是热层，requirements.txt 仍是本仓依赖真源：版本不同时这一层 pip
#     会覆盖安装，仅失去缓存收益。
ARG BASE_IMAGE=dops-base-python312:2026.11.1
FROM ${BASE_IMAGE}

# 默认值固定为国内源：部署脚本传与不传都不再击穿 pip 层缓存；
# 显式传空串仍回退官方源（兼容旧调用）。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

COPY requirements.txt /tmp/requirements.txt
# BuildKit 缓存挂载：wheel 常驻构建机，requirements 变更只下载新增包。
# 不要加 --no-cache-dir，否则 pip 下载缓存在层内被丢弃。
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$PIP_INDEX_URL" ]; then \
      python3 -m pip install -i "$PIP_INDEX_URL" -r /tmp/requirements.txt; \
    else \
      python3 -m pip install -r /tmp/requirements.txt; \
    fi

WORKDIR /opt/dops/repo
# 无 ENTRYPOINT/CMD：各服务在 compose 里指定各自的 python3 -m ... 入口。
