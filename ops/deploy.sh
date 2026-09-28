#!/usr/bin/env bash
# dops 拉模式 CD（全量迁云）：服务器自更新。
#
# 每台 dops 主机由 systemd timer（ops/dops-deploy.timer）周期触发本脚本快照，
# 从 git 远端拉取部署分支；有变更则（按需重建镜像）compose up -d + 健康门，
# 失败自动回滚到部署前 SHA。单机并发用 flock 去重。
#
# 为什么是拉模式而非 push 式 SSH：
#   天翼云 SG 拦 inbound 22（含 VPC 内，实测 dops-ci→dops-app:22 亦超时），
#   GitHub-hosted runner 无固定 IP 无法加白；而出向 443 到 GitHub 放开，
#   故由各主机自拉——零 SG 改动、三机统一、dops-ci 灾备天然成立。
#
# 边界：机密（/opt/dops/app.env、/opt/dops/live）在宿主机、不入库、本脚本不触碰。
# 可机器核验：每一步写 /opt/dops/logs/deploy.log，并落 /opt/dops/deploy-status.json。
set -euo pipefail

# ---- 可调（环境变量覆盖；systemd EnvironmentFile=/opt/dops/deploy.env） ----
REPO_DIR="${DOPS_REPO_DIR:-/opt/dops/repo}"
COMPOSE_FILE="${DOPS_COMPOSE_FILE:-/opt/dops/docker-compose.dops.yml}"
BRANCH="${DOPS_DEPLOY_BRANCH:-main}"
REMOTE="${DOPS_REMOTE:-origin}"
LOG_DIR="${DOPS_LOG_DIR:-/opt/dops/logs}"
LOCK_FILE="${DOPS_LOCK_FILE:-/var/run/dops-deploy.lock}"
STATUS_FILE="${DOPS_STATUS_FILE:-/opt/dops/deploy-status.json}"
HEALTH_URL="${DOPS_HEALTH_URL:-http://127.0.0.1:18080/healthz}"
HEALTH_TIMEOUT="${DOPS_HEALTH_TIMEOUT:-90}"
PIP_INDEX_URL="${DOPS_PIP_INDEX_URL:-}"
# 代码经 bind-mount 进容器的长驻服务（代码变更需 force-recreate 才重载）
CODE_SERVICES="${DOPS_CODE_SERVICES:-gateway scheduler bi-web}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/deploy.log"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG" >&2; }

write_status() { # $1=result $2=from $3=to $4=note
  cat >"$STATUS_FILE" <<JSON
{"ts":"$(date -Is)","result":"$1","from":"$2","to":"$3","branch":"$BRANCH","host":"$(hostname)","note":"$4"}
JSON
}

# 单实例防并发（timer 周期可能短于部署耗时）
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "SKIP: another deploy in progress"
  exit 0
fi

cd "$REPO_DIR"

git fetch --quiet "$REMOTE" "$BRANCH"
LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "$REMOTE/$BRANCH")"

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
  exit 0  # 无变更，静默（不打日志噪音）
fi

log "DEPLOY START: ${LOCAL_SHA:0:8} -> ${REMOTE_SHA:0:8} (branch=$BRANCH host=$(hostname))"

DEPS_CHANGED=0
git diff --quiet "$LOCAL_SHA" "$REMOTE_SHA" -- requirements.txt || DEPS_CHANGED=1

apply() {
  git reset --hard "$1"
  # compose 真源在 repo docker/cloud/，同步到 /opt/dops/ 部署副本
  if [ -f docker/cloud/docker-compose.dops.yml ]; then
    cp docker/cloud/docker-compose.dops.yml "$COMPOSE_FILE"
  fi
  if [ "$DEPS_CHANGED" = "1" ]; then
    log "requirements.txt changed -> rebuild dops-app image"
    docker build -t dops-app:latest \
      --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" \
      -f docker/cloud/Dockerfile .
  fi
  # 应用 compose/env 变更（nginx/ntp 等不强制重建）
  docker compose -f "$COMPOSE_FILE" up -d
  # 代码经 bind-mount：强制重建长驻 python 服务以重载代码
  # shellcheck disable=SC2086
  docker compose -f "$COMPOSE_FILE" up -d --force-recreate $CODE_SERVICES
}

health_gate() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      # 关键长驻容器均在 Up（bi-web 另看 healthz）
      if [ -z "$(docker ps \
          --filter name=dingtalk-gateway --filter name=dops-scheduler --filter name=dops-bi-web \
          --format '{{.Status}}' | grep -v Up)" ]; then
        return 0
      fi
    fi
    sleep 3
  done
  return 1
}

if apply "$REMOTE_SHA" && health_gate; then
  log "DEPLOY OK: $(git rev-parse --short HEAD)"
  write_status ok "$LOCAL_SHA" "$REMOTE_SHA" "deployed"
else
  log "DEPLOY FAIL -> rollback to ${LOCAL_SHA:0:8}"
  DEPS_CHANGED=0  # 回滚不重建镜像
  if apply "$LOCAL_SHA" && health_gate; then
    log "ROLLBACK OK: ${LOCAL_SHA:0:8}"
    write_status rolled_back "$REMOTE_SHA" "$LOCAL_SHA" "deploy failed, rolled back"
  else
    log "ROLLBACK FAILED: manual intervention required"
    write_status error "$REMOTE_SHA" "$LOCAL_SHA" "deploy and rollback both failed"
  fi
  exit 1
fi
