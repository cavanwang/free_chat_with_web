#!/bin/bash
# ================================================
# Midscene Computer 服务启动脚本
# 用于路线 A: OS 级视觉定位 + OS 级键鼠操作
# ================================================

MIDSCENE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$MIDSCENE_DIR"

# ============ 配置模型环境变量 ============
# 如果在 .env 文件中配置了,则自动加载
if [ -f ".env" ]; then
    echo "📋 从 .env 文件加载配置..."
    set -a
    source .env
    set +a
fi

# 如果环境变量未设置,使用这里的默认值(千问 DashScope)
export MIDSCENE_MODEL_API_KEY="${MIDSCENE_MODEL_API_KEY:-sk-8954849762d04e20875f963035ed25f6}"
export MIDSCENE_MODEL_NAME="${MIDSCENE_MODEL_NAME:-qwen3.7-plus}"
export MIDSCENE_MODEL_FAMILY="${MIDSCENE_MODEL_FAMILY:-qwen3}"
export MIDSCENE_MODEL_BASE_URL="${MIDSCENE_MODEL_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"

# ============ 1. 检查 Node.js ============
echo "🔍 检查 Node.js 环境..."
if ! command -v node &> /dev/null; then
    echo "❌ 未找到 node,请先安装 Node.js >= 18"
    exit 1
fi
NODE_VERSION=$(node -v)
echo "   Node.js: $NODE_VERSION"

# ============ 2. 检查依赖 ============
if [ ! -d "node_modules" ]; then
    echo "📦 首次运行,安装依赖..."
    npm install
    if [ $? -ne 0 ]; then
        echo "❌ 依赖安装失败"
        exit 1
    fi
fi

# ============ 3. 检查必要环境变量 ============
echo ""
echo "🔍 检查模型配置..."
missing=0

if [ -z "$MIDSCENE_MODEL_API_KEY" ]; then
    echo "   ⚠️  未设置 MIDSCENE_MODEL_API_KEY"
    missing=1
fi
if [ -z "$MIDSCENE_MODEL_NAME" ]; then
    echo "   ⚠️  未设置 MIDSCENE_MODEL_NAME"
    missing=1
fi
if [ -z "$MIDSCENE_MODEL_FAMILY" ]; then
    echo "   ⚠️  未设置 MIDSCENE_MODEL_FAMILY"
    missing=1
fi
if [ -z "$MIDSCENE_MODEL_BASE_URL" ]; then
    echo "   ⚠️  未设置 MIDSCENE_MODEL_BASE_URL"
    missing=1
fi

if [ $missing -eq 1 ]; then
    echo ""
    echo "❌ 缺少必要的模型配置,无法启动"
    echo ""
    echo "   请设置环境变量(或写入 .env 文件):"
    echo "   ┌─────────────────────────────────────────────────────┐"
    echo "   │ MIDSCENE_MODEL_API_KEY='your-api-key'               │"
    echo "   │ MIDSCENE_MODEL_NAME='qwen3.7-plus'                 │"
    echo "   │ MIDSCENE_MODEL_FAMILY='qwen3'                      │"
    echo "   │ MIDSCENE_MODEL_BASE_URL='https://dashscope.ali..."
    echo "   └─────────────────────────────────────────────────────┘"
    echo ""
    echo "   或者选择其他模型:"
    echo "   • 豆包 Seed (火山引擎): https://midscenejs.com/zh/model-doubao"
    echo "   • 阿里千问 (DashScope): https://midscenejs.com/zh/model-qwen"
    echo "   • 通用配置说明: https://midscenejs.com/zh/model-common-config"
    exit 1
fi

echo "   ✅ 模型配置已就绪"
echo "      模型: $MIDSCENE_MODEL_NAME ($MIDSCENE_MODEL_FAMILY)"
echo "      API : $MIDSCENE_MODEL_BASE_URL"
echo "      Key : ${MIDSCENE_MODEL_API_KEY:0:8}..."

# ============ 4. API 连通性检测 ============
echo ""
echo "🔌 测试 API 连通性..."
node test-api.js
if [ $? -ne 0 ]; then
    echo ""
    echo "❌ API 连通性检测失败,服务不会启动"
    echo "   请检查上面的错误信息,修复配置后重试"
    echo ""
    echo "   常见问题排查:"
    echo "   1. 确认 API Key 正确: echo \$MIDSCENE_MODEL_API_KEY"
    echo "   2. 确认模型名称正确: echo \$MIDSCENE_MODEL_NAME"
    echo "   3. 确认账户余额充足(阿里云/DashScope 控制台)"
    echo "   4. 确认网络可以访问 API 地址"
    echo ""
    echo "   快速验证命令:"
    echo "   curl -s $MIDSCENE_MODEL_BASE_URL/models \\"
    echo "     -H 'Authorization: Bearer $MIDSCENE_MODEL_API_KEY'"
    exit 1
fi

# ============ 5. 创建目录 ============
mkdir -p screenshots

# ============ 6. 启动服务 ============
echo ""
echo "🚀 启动 Midscene Computer 服务..."
PORT=${MIDSCENE_PORT:-3456}
echo "   端口: $PORT"
echo "   按 Ctrl+C 停止"
echo ""

node server.js
EXIT_CODE=$?

# ============ 服务退出 ============
echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "👋 Midscene 服务已停止"
else
    echo "⚠️  Midscene 服务异常退出 (exit code: $EXIT_CODE)"
fi

# ============ macOS 权限提示 ============
echo ""
echo "💡 如果鼠标/键盘无反应,请检查 macOS 辅助功能权限"
echo "   路径: 系统设置 → 隐私与安全 → 辅助功能"
echo "   为你的终端/IDE 勾选权限"
echo ""
echo "   如果拖拽偏离目标,可能是窗口偏移计算问题"
echo "   请确保 Chrome 浏览器窗口在最前面且未被遮挡"
