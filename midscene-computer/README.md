# Midscene Computer - OS 级滑块自动化(路线 A)

基于 `@midscene/computer` 的桌面级滑块自动化方案。

## 特性

- **纯视觉定位**:通过 AI 视觉模型识别滑块位置,零 DOM 依赖
- **OS 级操作**:鼠标移动/点击/拖拽走操作系统层面,浏览器 JS 层完全不可见
- **物理仿真轨迹**:Python 端生成人类化拖拽轨迹(过冲、回拉、Y 轴漂移),通过 Midscene 底层 API 逐点执行
- **自动回退**:Midscene 不可用时自动回退到 Playwright 方案

## 架构

```
Python (chat_with_qwen.py)
    │
    ├─ 1. 检测滑块出现(DOM 辅助)
    ├─ 2. 调用 Midscene /locate_slider → 获取 handle/gap 屏幕坐标
    ├─ 3. 生成物理仿真轨迹(points 数组)
    ├─ 4. 调用 Midscene /perform_drag → OS 级逐点拖拽
    └─ 5. 检查验证结果
         │
         ▼
Node.js (Midscene Computer 服务)
    ├─ POST /locate_slider  → agent.aiQuery('视觉定位滑块')
    ├─ POST /perform_drag  → agent.mouse.move/down/up(逐点)
    ├─ POST /click          → agent.mouse.click(OS 级点击)
    ├─ GET  /screenshot     → agent.screenshot(用于调试)
    ├─ GET  /screen_info    → agent.aiQuery(屏幕信息)
    └─ GET  /health         → 健康检查
```

## 快速开始

### 1. 配置模型环境变量

```bash
# 方式一:用阿里云 DashScope(qwen3.7-plus)
export MIDSCENE_MODEL_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export MIDSCENE_MODEL_API_KEY="your-dashscope-api-key"
export MIDSCENE_MODEL_NAME="qwen3.7-plus"
export MIDSCENE_MODEL_FAMILY="qwen3"

# 方式二:用 Doubao Seed(火山引擎)
export MIDSCENE_MODEL_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export MIDSCENE_MODEL_API_KEY="your-volcengine-api-key"
export MIDSCENE_MODEL_NAME="doubao-seed-2.1-turbo"
export MIDSCENE_MODEL_FAMILY="doubao-seed"
```

### 2. macOS 权限配置

Midscene 需要操作系统的键鼠控制权限:

1. 打开 **系统设置 > 隐私与安全 > 辅助功能**
2. 点击 **+** 号,添加你的终端应用(Terminal.app、iTerm2 或 VS Code)
3. 勾选已添加应用的权限

首次运行时会自动弹出权限请求。

### 3. 启动服务

```bash
cd midscene-computer
npm install  # 首次运行
./start.sh   # 启动服务
```

服务默认运行在 `http://127.0.0.1:3456`。

### 4. 在 Python 中启用

编辑 `chat_with_qwen.py`,设置:

```python
MIDSCENE_ENABLED = True  # 启用 Midscene OS 级操作
```

运行 `chat_with_qwen.py --api` 时,滑块验证会自动走 Midscene 路径。

## API 接口

### POST /locate_slider
视觉定位滑块位置。

**Body:**
```json
{
  "prompt": "屏幕上有一个滑块验证区域，找到滑块手柄和缺口的屏幕坐标"
}
```

**Response:**
```json
{
  "success": true,
  "handle": {"x": 680, "y": 520},
  "gap": {"x": 820, "y": 520}
}
```

### POST /perform_drag
OS 级逐点拖拽执行。

**Body:**
```json
{
  "points": [
    {"x": 670, "y": 520, "delayMs": 0},
    {"x": 680, "y": 520, "delayMs": 50},
    {"x": 700, "y": 521, "delayMs": 10},
    ...
  ],
  "startDelayMs": 150,
  "endDelayMs": 200
}
```

### GET /health
健康检查。

```json
{"status": "ok", "agentInitialized": true, "uptime": 42.5}
```

## 调试

### 手动截屏验证
```bash
curl http://127.0.0.1:3456/screenshot?filename=test.png
# 会保存到 midscene-computer/screenshots/test.png
```

### 检查屏幕分辨率
```bash
curl http://127.0.0.1:3456/screen_info
```

### 测试拖拽
```bash
curl -X POST http://127.0.0.1:3456/click \
  -H "Content-Type: application/json" \
  -d '{"x": 640, "y": 400}'
```

## 故障排查

| 问题 | 原因 | 解法 |
|---|---|---|
| `鼠标无反应` | macOS 未授权 | 系统设置 → 辅助功能 → 授权终端 |
| `服务启动失败` | 端口被占用或依赖未安装 | 检查端口 3456,重新 `npm install` |
| `AI 定位返回 0 个结果` | 模型配置错误或网络问题 | 检查 API Key 和 Base URL |
| `拖拽偏离目标` | 浏览器窗口被遮挡或偏移计算错误 | 确保 Chrome 在最前,或手动校准偏移 |
| `Midscene 调用超时` | 模型响应慢 | 增加 Python 端的 timeout |

## 文件结构

```
midscene-computer/
├── server.js          # Midscene HTTP 服务
├── start.sh           # 启动脚本
├── package.json       # npm 配置
├── screenshots/       # 截屏目录(调试用)
└── README.md          # 本文档
```

## 参考

- [Midscene 官网](https://midscenejs.com)
- [Midscene GitHub](https://github.com/web-infra-dev/midscene)
- [Desktop 文档](https://midscenejs.com/platforms/desktop)
- [模型配置](https://midscenejs.com/model-common-config)