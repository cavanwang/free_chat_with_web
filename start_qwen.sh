#!/bin/bash
# ================================================
# Qwen Chat 一键启动脚本
# 自动启动 Midscene OS 级服务 + Python 聊天
# 
# 用法:
#   ./start_qwen.sh                  # 交互式模式
#   ./start_qwen.sh --api            # API 模式 (端口 8765)
#   ./start_qwen.sh --api --port 9000
#   ./start_qwen.sh --no-midscene    # 不启动 Midscene
# ================================================

set -e

# ============ 路径配置 ============
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MIDSCENE_DIR="${SCRIPT_DIR}/midscene-computer"
PYTHON_SCRIPT="${SCRIPT_DIR}/chat_with_qwen.py"
LOG_DIR="${SCRIPT_DIR}/logs"

# 默认配置
API_PORT=8765
MIDSCENE_PORT=3456
ENABLE_MIDSCENE=true
API_MODE=false

# ============ Midscene 模型默认配置(千问 DashScope) ============
# 仅当 .env 文件未设置时使用这些默认值
export MIDSCENE_MODEL_API_KEY="${MIDSCENE_MODEL_API_KEY:-sk-8954849762d04e20875f963035ed25f6}"
export MIDSCENE_MODEL_NAME="${MIDSCENE_MODEL_NAME:-qwen3.7-plus}"
export MIDSCENE_MODEL_FAMILY="${MIDSCENE_MODEL_FAMILY:-qwen3}"
export MIDSCENE_MODEL_BASE_URL="${MIDSCENE_MODEL_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"

# ============ 解析参数 ============
while [[ $# -gt 0 ]]; do
    case "$1" in
        --api)
            API_MODE=true
            shift
            ;;
        --port)
            API_PORT="$2"
            shift 2
            ;;
        --no-midscene)
            ENABLE_MIDSCENE=false
            shift
            ;;
        --midscene-port)
            MIDSCENE_PORT="$2"
            shift 2
            ;;
        *)
            echo "未知参数: $1"
            echo "用法: $0 [--api] [--port PORT] [--no-midscene]"
            exit 1
            ;;
    esac
done

# ============ 创建日志目录 ============
mkdir -p "$LOG_DIR"

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

# ============ 检查 Midscene 服务是否已在运行 ============
check_midscene_running() {
    local port="${1:-$MIDSCENE_PORT}"
    if curl -s --connect-timeout 2 "http://127.0.0.1:${port}/health" > /dev/null 2>&1; then
        return 0  # 正在运行
    else
        return 1  # 未运行
    fi
}

# ============ 启动 Midscene 服务 ============
start_midscene() {
    if [ "$ENABLE_MIDSCENE" = "false" ]; then
        log_warn "Midscene 已禁用 (--no-midscene),跳过启动"
        return 0
    fi
    
    if check_midscene_running; then
        log_info "Midscene 服务已在运行 (端口 $MIDSCENE_PORT)"
        return 0
    fi
    
    log_step "启动 Midscene Computer 服务..."
    
    # 检查目录是否存在
    if [ ! -d "$MIDSCENE_DIR" ]; then
        log_error "Midscene 目录不存在: $MIDSCENE_DIR"
        log_error "请先初始化: cd midscene-computer && npm install"
        return 1
    fi
    
    # 检查 .env 文件
    if [ ! -f "${MIDSCENE_DIR}/.env" ] && [ ! -n "$MIDSCENE_MODEL_API_KEY" ]; then
        log_warn "未找到 .env 文件,可能无法调用 AI 模型"
        log_warn "请在 ${MIDSCENE_DIR}/.env 中配置 MIDSCENE_MODEL_API_KEY"
    fi
    
    # 检查端口是否被占用
    if lsof -i ":${MIDSCENE_PORT}" -sTCP:LISTEN > /dev/null 2>&1; then
        log_warn "端口 $MIDSCENE_PORT 已被占用,尝试终止旧进程..."
        lsof -ti ":${MIDSCENE_PORT}" -sTCP:LISTEN | xargs kill 2>/dev/null || true
        sleep 1
    fi
    
    # 后台启动 Midscene
    local log_file="${LOG_DIR}/midscene_$(date +%Y%m%d_%H%M%S).log"
    cd "$MIDSCENE_DIR"
    
    # 如果有 .env 文件,加载环境变量
    if [ -f ".env" ]; then
        set -a
        source .env
        set +a
    fi
    
    nohup node server.js > "$log_file" 2>&1 &
    local midscene_pid=$!
    
    log_info "Midscene PID: $midscene_pid (日志: $log_file)"
    
    # 等待服务就绪(最多 30 秒)
    log_info "等待 Midscene 服务就绪..."
    local waited=0
    local timeout=30
    
    while [ $waited -lt $timeout ]; do
        sleep 1
        waited=$((waited + 1))
        
        if check_midscene_running; then
            local health_data=$(curl -s --connect-timeout 1 "http://127.0.0.1:${MIDSCENE_PORT}/health" 2>/dev/null)
            local agent_init=$(echo "$health_data" | grep -o '"agentInitialized":true')
            
            if [ -n "$agent_init" ]; then
                log_info "✅ Midscene 服务就绪 (agent 已初始化) 耗时 ${waited}s"
            else
                log_info "✅ Midscene 服务就绪 (agent 未初始化,首次调用时自动加载) 耗时 ${waited}s"
            fi
            return 0
        fi
        
        # 检查进程是否还活着
        if ! kill -0 "$midscene_pid" 2>/dev/null; then
            log_error "Midscene 进程已退出,查看日志: $log_file"
            tail -20 "$log_file"
            return 1
        fi
    done
    
    log_error "Midscene 启动超时 (${timeout}s)"
    log_error "查看日志: $log_file"
    tail -20 "$log_file"
    return 1
}

# ============ 启动 Python 聊天 ============
start_chat() {
    log_step "启动 Qwen 聊天..."
    
    # 检查 Python 脚本是否存在
    if [ ! -f "$PYTHON_SCRIPT" ]; then
        log_error "Python 脚本不存在: $PYTHON_SCRIPT"
        return 1
    fi
    
    # 激活 Python 虚拟环境
    local venv_dir=""
    for candidate in ".venv" "venv" ".env" "env"; do
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
        log_warn "未找到虚拟环境,使用系统 Python"
        log_info "Python: $(which python3) ($(python3 --version 2>&1))"
    fi
    
    # 检查 Midscene 是否可用
    local midscene_args=""
    if [ "$ENABLE_MIDSCENE" = "true" ] && check_midscene_running; then
        midscene_args="--midscene true --midscene-url http://127.0.0.1:${MIDSCENE_PORT}"
        log_info "Midscene 已就绪,将使用 OS 级操作"
    else
        log_info "Midscene 未就绪,将使用 Playwright 操作"
    fi
    
    # 构建 Python 命令
    local cmd="python $PYTHON_SCRIPT"
    
    if [ "$API_MODE" = "true" ]; then
        cmd="$cmd --api --port $API_PORT $midscene_args"
        log_info "API 模式 (端口 $API_PORT)"
    else
        log_info "交互式模式"
        if [ -n "$midscene_args" ]; then
            # 交互式模式下传递 Midscene 参数,跳过询问
            cmd="$cmd $midscene_args"
        fi
    fi
    
    log_info "执行: $cmd"
    echo ""
    
    # 启动 Python 聊天
    if [ "$API_MODE" = "true" ]; then
        # API 模式:前台运行(Ctrl+C 退出)
        cd "$SCRIPT_DIR"
        eval $cmd
    else
        # 交互式模式:前台运行
        cd "$SCRIPT_DIR"
        eval $cmd
    fi
}

# ============ 清理函数 ============
cleanup() {
    echo ""
    log_info "正在退出..."
    
    # 如果 Python 是前台进程,它会先收到信号
    # Midscene 是后台进程,需要手动清理
    if [ "$ENABLE_MIDSCENE" = "true" ]; then
        local midscene_pid=$(lsof -ti ":${MIDSCENE_PORT}" -sTCP:LISTEN 2>/dev/null || true)
        if [ -n "$midscene_pid" ]; then
            log_info "终止 Midscene 进程 (PID: $midscene_pid)"
            kill "$midscene_pid" 2>/dev/null || true
            sleep 1
        fi
    fi
    
    log_info "已退出"
    exit 0
}

# ============ 注册清理钩子 ============
trap cleanup SIGINT SIGTERM

# ============ 主流程 ============
echo ""
echo "================================================"
echo "  Qwen Chat 一键启动"
echo "  Midscene OS 级反检测 + Python 聊天"
echo "================================================"
echo ""

# Step 1: 检查 Node.js
log_step "Step 1: 检查环境..."
if ! command -v node &> /dev/null; then
    log_error "未找到 Node.js,请先安装 >= 18"
    exit 1
fi
log_info "Node.js: $(node -v)"

if ! command -v python3 &> /dev/null; then
    log_error "未找到 python3"
    exit 1
fi
log_info "Python: $(python3 --version)"

# Step 2: 启动 Midscene
echo ""
log_step "Step 2: 处理 Midscene 服务..."
if [ "$ENABLE_MIDSCENE" = "true" ]; then
    if ! start_midscene; then
        log_warn "Midscene 启动失败,将使用 Playwright 继续"
        ENABLE_MIDSCENE=false
    fi
else
    log_info "Midscene 已禁用"
fi

# Step 3: 启动聊天
echo ""
log_step "Step 3: 启动聊天..."
echo ""

start_chat