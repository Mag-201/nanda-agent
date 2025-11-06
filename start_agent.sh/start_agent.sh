#!/usr/bin/env bash
set -Eeuo pipefail

### ====== 配置（可在命令前临时覆盖） ======
AGENT_ID="${AGENT_ID:-myagent}"
BRIDGE_PORT="${BRIDGE_PORT:-6000}"   # A2A bridge
UI_PORT="${UI_PORT:-5100}"           # Flask UI（避开 5000）
REGISTRY_URL="${REGISTRY_URL:-https://chat.nanda-registry.com:6900}"
PUBLIC_URL="${PUBLIC_URL:-https://arch-accurate-hanging-retired.trycloudflare.com}"  # 你的 Cloudflare URL
USE_TMUX="${USE_TMUX:-true}"         # true=tmux 后台跑；false=前台跑
TMUX_SESSION="agent_${AGENT_ID}"

### ====== 小工具 ======
log()   { printf "\033[1;34m[INFO]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
error() { printf "\033[1;31m[ERR ]\033[0m %s\n" "$*" >&2; exit 1; }

port_kill() {
  local p="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${p}/tcp" 2>/dev/null || true
  elif command -v lsof >/dev/null 2>&1; then
    local pid
    pid="$(lsof -t -iTCP:"$p" -sTCP:LISTEN || true)"
    [[ -n "${pid}" ]] && kill -9 $pid || true
  else
    warn "找不到 fuser/lsof，跳过端口 $p 释放"
  fi
}

### ====== 进入项目并激活 venv ======
PROJECT="${PROJECT_ROOT:-/home/ec2-user/nanda-agent}"
cd "$PROJECT" || error "没找到项目目录：$PROJECT"

if [[ -f ".venv311/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv311/bin/activate
  log "已激活虚拟环境 .venv311"
else
  warn "未发现 .venv311，使用系统 python3.11"
fi

### ====== 找脚本（优先新版 agents2）======
SCRIPT=""
if [[ -f "$PROJECT/agents2/run_ui_agent_https.py" ]]; then
  SCRIPT="$PROJECT/agents2/run_ui_agent_https.py"
elif [[ -f "$PROJECT/agents1/run_ui_agent_https.py" ]]; then
  SCRIPT="$PROJECT/agents1/run_ui_agent_https.py"
else
  # 兜底搜
  SCRIPT="$(find "$PROJECT" -type f -name 'run_ui_agent_https.py' -not -path '*/venv/*' -not -path '*/.venv/*' 2>/dev/null | head -n1 || true)"
fi
[[ -n "$SCRIPT" ]] || error "找不到 run_ui_agent_https.py"

log "使用脚本：$SCRIPT"

### ====== 环境变量导入（如果有 .env）======
if [[ -f ".env" ]]; then
  # 只导出简单的 KEY=VALUE 行，避免注释
  set -a
  # shellcheck disable=SC2046
  export $(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env | xargs -d '\n' -I{} sh -c 'echo {}')
  set +a
  log "已从 .env 加载环境变量"
fi

# 打印关键变量
log "AGENT_ID=$AGENT_ID"
log "BRIDGE_PORT=$BRIDGE_PORT"
log "UI_PORT=$UI_PORT"
log "REGISTRY_URL=$REGISTRY_URL"
log "PUBLIC_URL=$PUBLIC_URL"

### ====== 释放端口（可重复执行不冲突）======
log "释放端口 $BRIDGE_PORT / $UI_PORT（如占用）"
port_kill "$BRIDGE_PORT"
port_kill "$UI_PORT"

### ====== 生成启动命令 ======
CMD=( python3.11 "$SCRIPT"
  --id "$AGENT_ID"
  --port "$BRIDGE_PORT"
  --registry "$REGISTRY_URL"
  --public-url "$PUBLIC_URL"
  --api-port "$UI_PORT"
)

log "启动命令：${CMD[*]}"

### ====== 启动（tmux 或前台）======
if [[ "$USE_TMUX" == "true" ]]; then
  if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    warn "已有 tmux 会话：$TMUX_SESSION，先杀掉"
    tmux kill-session -t "$TMUX_SESSION" || true
  fi
  tmux new -d -s "$TMUX_SESSION" "${CMD[*]}"
  log "已在 tmux 会话 $TMUX_SESSION 中启动。查看日志： tmux attach -t $TMUX_SESSION"
else
  "${CMD[@]}"
fi

### ====== 健康检查 ======
sleep 2
UI_HEALTH="$(curl -sS -m 3 "http://127.0.0.1:${UI_PORT}/api/health" || true)"
if [[ -n "$UI_HEALTH" ]]; then
  log "UI 健康检查通过：$UI_HEALTH"
else
  warn "UI 健康检查失败：http://127.0.0.1:${UI_PORT}/api/health"
fi

A2A_OK="$(curl -sS -m 3 -X POST "http://127.0.0.1:${BRIDGE_PORT}/a2a" -H 'Content-Type: application/json' -d '{"message":"ping"}' || true)"
if [[ -n "$A2A_OK" ]]; then
  log "Bridge(A2A) 本地可达：$A2A_OK"
else
  warn "Bridge(A2A) 本地不可达：http://127.0.0.1:${BRIDGE_PORT}/a2a"
fi

log "如需公网连通，单开一个窗口运行： cloudflared tunnel --url http://localhost:${BRIDGE_PORT}"
log "完成。"
