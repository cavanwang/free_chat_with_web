#!/bin/bash
# ================================================
# DeepSeek Chat 一键启动脚本
# 默认按“交互(聊天)模式”启动 chat_deepseek_web.py, 而非 API 模式
#
# 用法:
#   ./start_deepseek.sh              # 交互聊天模式(默认)
#   ./start_deepseek.sh --api        # API 模式(OpenAI 兼容, 默认端口 8765)
#   ./start_deepseek.sh --api --port 9000
#   ./start_deepseek.sh --help
#
# 说明:
#   - DeepSeek 使用自带 Chrome profile + CDP(端口 9222), 不需要 Midscene
#   - 每次启动都会清空 run.log, 保证 run.log 始终是本次启动期间的日志
# ================================================
set -e

# ============ 路径配置 ============
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/chat_deepseek_web.py"
RUN_LOG="${SCRIPT_DIR}/run.log"

# ============ 默认配置 ============
API_PORT=8765
API_MODE=false
CLEAR_HISTORY=false
RANDOM_SESSIONID=false

# ============ 解析参数 ============
while [[ $# -gt 0 ]]; do
    case "$1" in
        --api)
            API_MODE=true
            shift
            ;;
        --clear-history)
            CLEAR_HISTORY=true
            shift
            ;;
        --random-sessionid)
            RANDOM_SESSIONID=true
            shift
            ;;
        --port)
            API_PORT="$2"
            shift 2
            ;;
        -h|--help)
            echo "用法: $0 [--api] [--port PORT]"
            echo "  (无参数)      交互聊天模式(默认)"
            echo "  --api         启动 OpenAI 兼容 API 服务"
            echo "  --port PORT   API 监听端口(默认 8765, 仅 --api 生效)"
            echo "  --clear-history  启动时先清空全部历史会话, 再开一个干净新会话"
            echo "  --random-sessionid  每次启动使用独立的随机默认会话(不带会话id的请求纯新且互不串味)"
            exit 0
            ;;
        *)
            echo "未知参数: $1"
            echo "用法: $0 [--api] [--port PORT]"
            exit 1
            ;;
    esac
done

# ============ 彩色输出函数 ============
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${CYAN}[STEP]${NC} $*"; }

# ============ 主流程 ============
echo ""
echo "================================================"
echo "  DeepSeek Chat 一键启动"
echo "================================================"
echo ""

# Step 1: 检查 Python 脚本
log_step "Step 1: 检查环境..."
if [ ! -f "$PYTHON_SCRIPT" ]; then
    log_error "Python 脚本不存在: $PYTHON_SCRIPT"
    exit 1
fi
if ! command -v python3 &> /dev/null; then
    log_error "未找到 python3"
    exit 1
fi
log_info "Python 脚本: $PYTHON_SCRIPT"

# Step 2: 清空 run.log(确保 run.log 始终是本次启动期间的日志)
echo ""
log_step "Step 2: 清空 run.log..."
: > "$RUN_LOG"
log_info "run.log 已清空: $RUN_LOG"

# Step 3: 激活 Python 虚拟环境
echo ""
log_step "Step 3: 准备 Python 环境..."
venv_dir=""
for candidate in ".venv" "venv" "env"; do
    if [ -f "${SCRIPT_DIR}/${candidate}/bin/activate" ]; then
        venv_dir="${SCRIPT_DIR}/${candidate}"
        break
    fi
done
if [ -n "$venv_dir" ]; then
    log_info "激活虚拟环境: $venv_dir"
    # shellcheck disable=SC1091
    source "${venv_dir}/bin/activate"
    log_info "Python: $(which python) ($(python --version 2>&1))"
else
    log_warn "未找到虚拟环境, 使用系统 Python"
    log_info "Python: $(which python3) ($(python3 --version 2>&1))"
fi

# Step 4: 启动
echo ""
log_step "Step 4: 启动 DeepSeek 聊天..."
cd "$SCRIPT_DIR"
if [ "$API_MODE" = "true" ]; then
    log_info "启动模式: API 模式 (端口 $API_PORT)"
    CMD="python \"$PYTHON_SCRIPT\" --api --port $API_PORT"
else
    log_info "启动模式: 交互聊天模式 (默认)"
    CMD="python \"$PYTHON_SCRIPT\""
fi
if [ "$CLEAR_HISTORY" = "true" ]; then
    CMD="$CMD --clear-history"
    log_info "已启用: 启动前清空全部历史会话 (--clear-history)"
fi
if [ "$RANDOM_SESSIONID" = "true" ]; then
    CMD="$CMD --random-sessionid"
    log_info "已启用: 每次启动使用独立随机默认会话 (--random-sessionid)"
fi
log_info "执行: $CMD"
echo ""

# 前台运行(Ctrl+C 退出)。日志由 Python 侧的 log() 同时写入 run.log
eval $CMD
