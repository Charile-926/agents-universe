#!/usr/bin/env bash
#
# 服务器一键发布：备份 → 拉代码 → 重建镜像 → 重启 → 健康检查 → 失败自动回滚。
#
# 用法（在服务器上，仓库根目录执行）：
#   bash scripts/deploy_server.sh              # 正常发布
#   bash scripts/deploy_server.sh --dry-run    # 只打印将执行的动作，不做任何修改
#   bash scripts/deploy_server.sh --help
#
# 从本机一条命令触发：
#   ssh root@<server> "cd /root/agents-universe && bash scripts/deploy_server.sh"
#
# 可用环境变量覆盖：
#   APP_DIR=/root/agents-universe   仓库目录（默认脚本所在仓库根）
#   BRANCH=main                     要部署的分支
#   IMAGE=agents-universe-api:latest 镜像名
#   SERVICE=api                     compose 服务名
#   HEALTH_URL=http://127.0.0.1:8000/health
#   HEALTH_TIMEOUT=120              健康检查最长等待秒数
#   KEEP_BACKUPS=5                  保留最近几份备份
#
# 设计要点：
# 1. 前端与后端都打进镜像，所以必须重新 docker build，不能只重启容器。
#    镜像的 `final` 阶段是 Dockerfile 的最后一个 stage（默认 target），脚本仍显式
#    写 --target final 作为双保险。
# 2. 升级会跑 alembic 迁移（删列、重建表）。SQLite 部署的回滚必须"旧镜像 + 旧库文件"
#    一起恢复，只回滚镜像会因为缺列而 500；PostgreSQL 部署请自行确认数据库已有快照。
# 3. SQLite 库文件必须在挂载卷里。脚本会核对容器路径与宿主机路径的映射，
#    对不上就中止，避免容器重建把库文件一起删掉。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BRANCH="${BRANCH:-main}"
IMAGE="${IMAGE:-agents-universe-api:latest}"
SERVICE="${SERVICE:-api}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"

DRY_RUN=0
SKIP_DB_BACKUP=0
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$APP_DIR/backups/$TS"
BACKUP_TAG="${IMAGE%%:*}:backup-$TS"
DB_BACKUP=""
DB_HOST_PATH=""

log()  { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '\033[90m[dry-run]\033[0m %s\n' "$*"
  else
    eval "$@"
  fi
}

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed '${/^$/d;}'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skip-db-backup) SKIP_DB_BACKUP=1 ;;
    -h|--help) usage ;;
    *) die "未知参数：$arg（用 --help 查看用法）" ;;
  esac
done

# ── 0. 前置检查 ─────────────────────────────────────────────────────────────
cd "$APP_DIR" || die "进不去仓库目录：$APP_DIR"
[ -d .git ] || die "$APP_DIR 不是 git 仓库"
[ -f .env ] || die "缺少 .env（先 cp .env.example .env 并填 SECRET_KEY / DATABASE_URL）"
[ -f docker-compose.yml ] || die "缺少 docker-compose.yml（先 cp docker-compose.example.yml docker-compose.yml）"
command -v docker >/dev/null || die "未安装 docker"
docker compose version >/dev/null 2>&1 || die "docker compose 不可用"

env_value() {
  # 取 .env 中某个键的值（忽略注释、行尾空白与可能的 CRLF 回车）
  grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- \
    | tr -d '\r' | sed 's/[[:space:]]*$//' || true
}

SECRET_KEY="$(env_value SECRET_KEY)"
DATABASE_URL="$(env_value DATABASE_URL)"
BYPASS="$(env_value AUTH_BYPASS_ENABLED)"

[ -n "$SECRET_KEY" ] || die ".env 缺少 SECRET_KEY"
case "$SECRET_KEY" in
  change-me*) die "SECRET_KEY 仍是占位值，拒绝发布" ;;
esac
[ -n "$DATABASE_URL" ] || die ".env 缺少 DATABASE_URL。
  本版本默认存储是 PostgreSQL；服务器若继续用 SQLite，必须显式写：
  DATABASE_URL=sqlite+aiosqlite:////app/data/<库文件>.db"
if [ "${BYPASS,,}" != "false" ]; then
  die "AUTH_BYPASS_ENABLED 必须为 false（当前：${BYPASS:-未设置}）。
  否则所有请求都会归到同一个内置用户，账号与会话隔离失效。"
fi

log "仓库：$APP_DIR  分支：$BRANCH  镜像：$IMAGE  服务：$SERVICE"

# ── 1. 解析 SQLite 库文件位置，并映射回宿主机路径 ───────────────────────────
case "$DATABASE_URL" in
  sqlite*)
    # sqlite+aiosqlite:////app/data/x.db → /app/data/x.db
    # sqlite+aiosqlite:///data/x.db     → data/x.db（相对路径，不支持）
    CT_PATH="${DATABASE_URL#*://}"
    CT_PATH="${CT_PATH#/}"
    case "$CT_PATH" in
      /*) ;;
      *) die "DATABASE_URL 里的 SQLite 路径不是绝对路径：$CT_PATH" ;;
    esac
    log "检测到 SQLite：容器内路径 $CT_PATH"
    warn "使用 SQLite 时，docker-compose.yml 里 $SERVICE 的 depends_on 不应再包含 postgres/sqlserver（只保留 redis），否则 up -d 会连带拉起数据库服务"

    CID="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"
    if [ -z "$CID" ]; then
      warn "服务 $SERVICE 当前没有运行中的容器，跳过库文件备份（首次部署属正常）"
      SKIP_DB_BACKUP=1
    else
      MOUNTS="$(docker inspect -f '{{range .Mounts}}{{.Destination}}={{.Source}}{{"\n"}}{{end}}' "$CID" || true)"
      BEST_DEST=""
      BEST_SRC=""
      while IFS='=' read -r dest src; do
        [ -n "${dest:-}" ] || continue
        case "$CT_PATH" in
          "$dest"/*)
            if [ "${#dest}" -gt "${#BEST_DEST}" ]; then
              BEST_DEST="$dest"
              BEST_SRC="$src"
            fi
            ;;
        esac
      done <<< "$MOUNTS"

      if [ -z "$BEST_DEST" ]; then
        die "SQLite 库文件不在任何挂载卷内（容器路径 $CT_PATH）。
  容器重建会连同库文件一起丢失。请先在 docker-compose.yml 的 $SERVICE 服务加上：
    volumes:
      - ./\${DOCKER_VOLUME_ROOT:-./data}/data:/app/data
  然后迁移现有库文件，再重新执行本脚本。"
      fi
      DB_HOST_PATH="${BEST_SRC}${CT_PATH#"$BEST_DEST"}"
      [ -f "$DB_HOST_PATH" ] || warn "宿主机上没找到库文件 $DB_HOST_PATH（首次部署可忽略）"
      log "库文件映射：$CT_PATH → $DB_HOST_PATH"
    fi
    ;;
  *)
    log "非 SQLite 数据库，跳过文件备份（请自行确认数据库已有快照）"
    ;;
esac

# ── 2. 备份：库文件 + 当前镜像 ─────────────────────────────────────────────
CURRENT_IMAGE_ID="$(docker images -q "$IMAGE" 2>/dev/null | head -1 || true)"
if [ -n "$CURRENT_IMAGE_ID" ]; then
  log "备份当前镜像 → $BACKUP_TAG"
  run "docker tag '$IMAGE' '$BACKUP_TAG'"
else
  warn "本地还没有 $IMAGE，首次构建无需回滚镜像"
fi

if [ "$SKIP_DB_BACKUP" = "0" ] && [ -n "$DB_HOST_PATH" ] && [ -f "$DB_HOST_PATH" ]; then
  run "mkdir -p '$BACKUP_DIR'"
  run "cp -a '$DB_HOST_PATH' '$BACKUP_DIR/'"
  DB_BACKUP="$BACKUP_DIR/$(basename "$DB_HOST_PATH")"
  log "备份库文件 → $DB_BACKUP"
fi

# ── 3. 拉代码（.env / docker-compose.yml / data 均未跟踪，不会被覆盖）───────
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  warn "工作区有未提交的改动，git reset --hard 会丢弃："
  git status --short --untracked-files=no | sed 's/^/         /'
fi
log "拉取 origin/$BRANCH"
# 显式 refspec：只写 `git fetch origin main` 时，若服务器没有配置
# remote.origin.fetch，只会更新 FETCH_HEAD 而不更新 origin/main，
# 后面的 reset 就会停在旧提交（部署"成功"却还是旧代码）。
run "git fetch origin '$BRANCH:refs/remotes/origin/$BRANCH'"
run "git reset --hard 'origin/$BRANCH'"

# ── 4. 构建镜像（final 是 Dockerfile 的默认 target；显式写出作为双保险）──────
log "构建镜像（前端与后端都打进镜像，首次约 10–30 分钟）"
run "docker build --target final -t '$IMAGE' ."

# ── 5. 重启服务并等待健康检查 ─────────────────────────────────────────────
rollback() {
  warn "发布失败，开始回滚"
  if [ -n "$CURRENT_IMAGE_ID" ]; then
    run "docker tag '$BACKUP_TAG' '$IMAGE'"
  fi
  if [ -n "$DB_BACKUP" ] && [ -f "$DB_BACKUP" ]; then
    warn "恢复库文件：$DB_BACKUP → $DB_HOST_PATH"
    run "cp -a '$DB_BACKUP' '$DB_HOST_PATH'"
  fi
  run "docker compose up -d '$SERVICE'"
  warn "回滚完成。最近日志："
  docker compose logs --tail=80 "$SERVICE" || true
  exit 1
}

log "重启 $SERVICE"
run "docker compose up -d '$SERVICE'" || rollback

if [ "$DRY_RUN" = "1" ]; then
  log "dry-run 结束，未做任何修改"
  exit 0
fi

log "等待健康检查（最长 ${HEALTH_TIMEOUT}s）：$HEALTH_URL"
DEADLINE=$(( $(date +%s) + HEALTH_TIMEOUT ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    log "健康检查通过"
    break
  fi
  sleep 3
done
if ! curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
  warn "健康检查超时"
  docker compose logs --tail=120 "$SERVICE" || true
  rollback
fi

# ── 6. 清理旧备份（保留最近 N 份）──────────────────────────────────────────
if [ -d "$APP_DIR/backups" ]; then
  ls -1dt "$APP_DIR"/backups/*/ 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | while read -r old; do
    log "清理旧备份：$old"
    rm -rf "$old"
  done
fi

log "发布完成。浏览器 Ctrl+F5 强刷即可看到新版。"
if [ -n "$DB_BACKUP" ]; then
  log "回滚用备份：镜像 $BACKUP_TAG；库文件 $DB_BACKUP"
fi
exit 0
