# Free Chat With Web

通过 Hook 方式实现的免费 Web 聊天工具,支持 DeepSeek 和阿里千问两大平台,提供交互式终端和 OpenAI 兼容 API 两种使用方式。

## 功能特性

- **双平台支持**: DeepSeek 深度思考、阿里千问(含思考过程解析)
- **反爬模拟**: 人类化输入(对数正态分布击键间隔、标点减速、过冲回调)
- **会话管理**: 保持上下文 / 按需重置会话(新标签页)
- **Token 估算**: API 响应中包含 token 消耗统计(含 reasoning tokens)
- **思考过程**: 解析并输出模型的思考过程(reasoning content)
- **模型/模式切换**: 交互式终端支持切换模型和对话模式
- **自动滑块**: 视觉定位 + 人类化拖动(失败回退手动)
- **端口隔离**: DeepSeek(9222)和千问(9223)使用独立 Chrome 调试端口
- **Stealth 补丁**: 隐藏 webdriver、模拟 chrome API、permissions 等反检测

## 项目结构

```
free_chat_with_web/
├── chat_deepseek_web.py   # DeepSeek Web Hook
├── chat_with_qwen.py      # 千问 Web Hook
├── requirements.txt       # Python 依赖
├── README.md
├── deepseek_chrome_profile/  # DeepSeek Chrome 用户数据目录
├── qwen_chrome_profile/      # 千问 Chrome 用户数据目录(运行时生成)
├── raw_dumps/                # DeepSeek 原始响应 dump
└── raw_dumps_qwen/           # 千问原始响应 dump
```

## 环境要求

- Python 3.9+
- macOS / Linux / Windows
- Google Chrome 浏览器
- Chrome 已登录 DeepSeek 和千问(通过 Chrome Profile 保持登录状态)

## 安装

```bash
# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
pip install opencv-python-headless numpy  # 千问自动滑动需要
```

## DeepSeek

### 交互式终端

```bash
python chat_deepseek_web.py
```

交互式命令（输入 `/` 弹出 TUI 命令菜单，方向键选择，回车确认）:
```
You: <输入消息>     # 发送聊天
/help              # 查看所有命令
/chatmodes         # 查看对话模式
/mode <模式名>      # 切换对话模式 (快速模式 / 专家模式 / 识图模式)
/quit              # 退出
```

> 提示: 直接输入 `/` 即可弹出命令列表悬浮菜单；旧的无前缀命令（`chatmodes`、`mode`、`quit`）仍兼容可用。

### API 服务器模式

```bash
python chat_deepseek_web.py --api --port 8000
```

**API 端点:**

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 聊天(支持流式和非流式) |
| `/v1/models` | GET | 模型列表 |

**请求示例:**
```bash
# 流式聊天
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":true}'

# 非流式
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":false}'
```

**响应格式(兼容 OpenAI):**
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "deepseek-web",
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 200,
    "reasoning_tokens": 50,
    "total_tokens": 350
  },
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "回复正文",
      "reasoning_content": "思考过程"
    }
  }]
}
```

## 千问

### 交互式终端

```bash
python chat_with_qwen.py
```

交互式命令（输入 `/` 弹出 TUI 命令菜单，方向键选择，回车确认）:
```
You: <输入消息>     # 发送聊天
/help              # 查看所有命令
/models            # 查看可用模型列表
/model <模型名>     # 切换模型
/chatmodes         # 查看对话模式
/chatmode <模式名>  # 切换对话模式
/quit              # 退出
```

> 提示: 直接输入 `/` 即可弹出命令列表悬浮菜单；旧的无前缀命令（`models`、`model`、`chatmodes`、`chatmode`、`quit`）仍兼容可用。

### API 服务器模式

```bash
python chat_with_qwen.py --api --port 8765
```

**API 端点:**

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 聊天 |
| `/v1/session/reset` | POST | 重置会话(新标签页) |
| `/v1/models` | GET | 模型列表 |

**会话管理:**

默认保持当前会话(有上下文)。通过 `new_session` 参数或独立接口重置:

```bash
# 保持当前会话(有上下文)
curl -N -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}]}'

# 新建会话(重置上下文)
curl -N -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"new_session":true}'

# 独立重置会话
curl -X POST http://localhost:8765/v1/session/reset
```

## 工作原理

```
┌─────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  客户端      │────▶│  HTTP API 服务    │────▶│  Chrome 进程     │
│ curl/Client  │     │  (aiohttp)       │     │  (CDP 远程调试)  │
└─────────────┘     └─────────────────┘     └─────────────────┘
                           │                         │
                           ▼                         ▼
                    请求解析 + 锁             Playwright 自动化
                    会话管理                    │
                                               ▼
                                        ┌─────────────┐
                                        │  Hook JS    │
                                        │ (fetch/XHR  │
                                        │  拦截)      │
                                        └─────────────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │  SSE 响应   │
                                        │ (正文+思考) │
                                        └─────────────┘
```

## 技术细节

### Hook 机制
- 注入 JavaScript 拦截 `fetch` 和 `XMLHttpRequest`
- 通过 `CDP Runtime.addBinding` 将响应数据传回 Python
- 支持 SSE 流式响应解析,按 mime_type 区分正文和思考过程

### 人类化输入
- **击键间隔**: 对数正态分布(中位数 ~80ms, σ=0.35)
- **标点减速**: 标点字符前延迟 1.8 倍
- **句末停顿**: 句末标点后 300-800ms 长停顿
- **分段输入**: 每 2-3 字一组,组间 180-520ms 间隔

### 反爬策略
- Stealth 补丁: `navigator.webdriver`、`chrome.runtime`、`plugins`、`permissions`
- 真人拖动: 钟形速度曲线 + Y 轴抖动 + 过冲回调
- 保持会话: 真实 Chrome 进程 + 用户 Profile

## 端口配置

| 服务 | 调试端口 | API 端口 |
|------|---------|---------|
| DeepSeek | 9222 | 8000 |
| 千问 | 9223 | 8765 |

## 注意事项

- 需要保持 Chrome 登录状态,首次使用会弹出登录页
- 滑块验证可能需要手动完成(自动滑动失败时)
- API 模式下所有请求串行处理(通过 `asyncio.Lock` 保证安全)
- Chrome 窗口关闭后脚本会检测并提示重启

## License

MIT
