#!/usr/bin/env bash
# 线上渠道独立拉模式 CD：独立 worktree/镜像/锁/状态，只重建 channel-scheduler。
# 数据库与 Nacos 是共享控制面：迁移只允许 expand-only，不回滚 DB；发布不执行
# publish-pipelines，避免覆盖生产启停、cron 与 params。首次切换先部署主 dops
# （主调度排除渠道任务），再启用本服务，避免双调度；MySQL 命名锁仍是最后防线。
set -euo pipefail

REPO_DIR="${DOPS_CHANNEL_REPO_DIR:-/opt/dops/channel-repo}"
COMPOSE_FILE="${DOPS_CHANNEL_COMPOSE_FILE:-/opt/dops/docker-compose.channel.yml}"
BRANCH="${DOPS_CHANNEL_DEPLOY_BRANCH:-main}"
REMOTE="${DOPS_CHANNEL_REMOTE:-origin}"
LOG_DIR="${DOPS_CHANNEL_LOG_DIR:-/opt/dops/logs}"
LOCK_FILE="${DOPS_CHANNEL_LOCK_FILE:-/var/run/dops-channel-deploy.lock}"
STATUS_FILE="${DOPS_CHANNEL_STATUS_FILE:-/opt/dops/channel-deploy-status.json}"
HEALTH_TIMEOUT="${DOPS_CHANNEL_HEALTH_TIMEOUT:-90}"
PIP_INDEX_URL="${DOPS_CHANNEL_PIP_INDEX_URL:-${DOPS_PIP_INDEX_URL:-}}"

mkdir -p "$LOG_DIR" "$(dirname "$STATUS_FILE")"
LOG="$LOG_DIR/channel-deploy.log"
RELEASE_ID="channel-$(date +%Y%m%d%H%M%S)"
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG" >&2; }

write_status() { # result from to note
  python3 - "$STATUS_FILE" "$RELEASE_ID" "$1" "$2" "$3" "$BRANCH" "$4" <<'PY'
import json
import socket
import sys
from datetime import datetime, timezone

path, release_id, result, from_sha, to_sha, branch, note = sys.argv[1:]
payload = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "release_id": release_id,
    "release": "online-channel",
    "result": result,
    "from": from_sha,
    "to": to_sha,
    "branch": branch,
    "host": socket.gethostname(),
    "services": ["channel-scheduler"],
    "note": note,
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False)
PY
}

if [ ! -d "$REPO_DIR/.git" ]; then
  log "FATAL: $REPO_DIR 不是独立 git worktree/clone；先按切换步骤初始化"
  exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "SKIP: another channel deploy in progress"
  exit 0
fi

cd "$REPO_DIR"
git fetch --quiet "$REMOTE" "$BRANCH"
LOCAL_SHA="$(git rev-parse HEAD)"
# REMOTE 既支持具名远端，也支持带凭据的私有仓 URL；两者统一读 FETCH_HEAD。
REMOTE_SHA="$(git rev-parse FETCH_HEAD)"
RELEASE_ID="${RELEASE_ID}-${REMOTE_SHA:0:12}"

DEPS_CHANGED=0
git diff --quiet "$LOCAL_SHA" "$REMOTE_SHA" -- requirements.txt || DEPS_CHANGED=1
if ! docker image inspect dops-channel-app:latest >/dev/null 2>&1; then
  DEPS_CHANGED=1  # 首次安装即使同 SHA 也必须先建渠道镜像。
fi
if [ "$LOCAL_SHA" = "$REMOTE_SHA" ] \
    && [ "$DEPS_CHANGED" = "0" ] \
    && [ -f "$COMPOSE_FILE" ] \
    && [ "$(docker inspect -f '{{.State.Running}}' dops-channel-scheduler 2>/dev/null || true)" = "true" ]; then
  exit 0
fi

log "CHANNEL DEPLOY START: $RELEASE_ID ${LOCAL_SHA:0:8} -> ${REMOTE_SHA:0:8}"

apply() {
  # 先停调度再换代码，杜绝 bind-mount 下旧进程派生新代码子进程的混合版本。
  docker compose -f "$COMPOSE_FILE" stop channel-scheduler >/dev/null 2>&1 || true
  git reset --hard "$1"
  cp docker/cloud/docker-compose.channel.yml "$COMPOSE_FILE"
  if [ "$DEPS_CHANGED" = "1" ]; then
    log "requirements.txt changed -> rebuild dops-channel-app:latest"
    docker build -t dops-channel-app:latest \
      --build-arg "PIP_INDEX_URL=$PIP_INDEX_URL" \
      -f docker/cloud/Dockerfile .
  fi
  # 用新代码的一次性容器补迁移，再启动调度；失败进入代码回滚，DB 不反向回滚。
  docker compose -f "$COMPOSE_FILE" run --rm --no-deps \
    --entrypoint python3 channel-scheduler \
    -m common.public_data.cli migrate
  docker compose -f "$COMPOSE_FILE" up -d --force-recreate channel-scheduler
}

health_gate() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ "$(docker inspect -f '{{.State.Running}}' dops-channel-scheduler 2>/dev/null || true)" = "true" ]; then
      if docker compose -f "$COMPOSE_FILE" exec -T channel-scheduler \
          python3 ops/channel_health.py >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 3
  done
  return 1
}

if apply "$REMOTE_SHA" && health_gate; then
  log "CHANNEL DEPLOY OK: $RELEASE_ID $(git rev-parse --short HEAD)"
  write_status ok "$LOCAL_SHA" "$REMOTE_SHA" "channel scheduler deployed and health gate passed"
else
  log "CHANNEL DEPLOY FAIL -> rollback to ${LOCAL_SHA:0:8}"
  if apply "$LOCAL_SHA" && health_gate; then
    log "CHANNEL ROLLBACK OK: $RELEASE_ID ${LOCAL_SHA:0:8}"
    write_status rolled_back "$REMOTE_SHA" "$LOCAL_SHA" "channel deploy failed; code and image rolled back; DB/Nacos unchanged"
  else
    log "CHANNEL ROLLBACK FAILED: manual intervention required"
    write_status error "$REMOTE_SHA" "$LOCAL_SHA" "channel deploy and rollback both failed"
  fi
  exit 1
fi
