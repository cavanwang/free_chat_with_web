# -*- coding: utf-8 -*-
"""Qwen 网关配置常量。"""
from pathlib import Path

# 项目根目录(包上一级)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ============ 浏览器配置 ============
CHROME_PATH = r"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEBUG_PORT = 9223
USER_DATA_DIR = Path("./qwen_chrome_profile")
QWEN_URL = "https://www.qianwen.com/"
QWEN_HOST = "qianwen.com"
HEADLESS = False  # 默认有头模式, 可实时看到画面并手动交互
                  # OS 级操作已锁定禁用, 不会干扰宿主机
LOGIN_TIMEOUT_SEC = 600
REUSE_EXISTING_TAB = True
DUMP_RAW = True
DUMP_RAW_SSE = True  # 保存原始 SSE 行(含所有 mime_type),用于诊断思考过程
RAW_DUMP_DIR = Path("./raw_dumps_qwen")
STRIP_CITATIONS = False
STRIP_THINK_REF_TAGS = True  # 剥离正文中的 [(multimodal_chat_think_N)] 引用标签
STREAM_OUTPUT = True
PRINT_STREAM_PREFIX = True
ENABLE_MODES = ["思考研究"]
DEFAULT_MODEL = ""

# ============ Midscene OS 级自动化配置(路线 A) ============
MIDSCENE_BASE_URL = "http://127.0.0.1:3456"  # Midscene Node.js 服务地址
# MIDSCENE_ENABLED 动态获取:
#   1. 命令行 --midscene true/false
#   2. 环境变量 MIDSCENE_ENABLED=true/false
#   3. 交互式询问(仅非 API 模式)
MIDSCENE_ENABLED = False  # 初始值,启动时动态设置
_MIDSCENE_EXTERNAL_SET = False  # 是否已通过命令行/环境变量确定

# 安全策略: Midscene OS 级操作默认锁定为禁用
#   - 有头模式下: 可以通过交互式询问或 --midscene true 显式开启
#   - 无头模式下: 强制锁定, 完全禁用 (无头模式下 OS 级操作无意义且危险)
if HEADLESS:
    MIDSCENE_ENABLED = False
    _MIDSCENE_EXTERNAL_SET = True  # 阻止交互式询问

# ============ 调试截图存档 ============
DEBUG_SCREENSHOT_DIR = Path("./debug_screenshots")  # 关键节点自动截图保存目录
DEBUG_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


# ============ API 配置 ============
API_HOST = "0.0.0.0"
API_PORT = 8765
API_KEY = ""  # 留空则不鉴权
API_MODEL = "qwen-web"

# ============ 会话摘要/压缩配置(网关持有会话正本) ============
# 会话历史正本由网关按 conversation_id 存于共享 SQLite, 与浏览器解耦。
# 每次 API 调用: 点"新建对话"开干净会话 -> 拼装[摘要+最近K轮+本轮] -> fill 注入。
SESSION_DB_PATH = str(_PROJECT_ROOT / "sessions.db")
DEFAULT_CONVERSATION_ID = "default"   # 客户端不传 conversation_id 时的兜底会话
COMPACT_SOFT_LIMIT = 24000            # 会话累计估算 token 超过此值触发压缩
COMPACT_KEEP_RECENT = 3              # 压缩时保留最近的轮数(user+assistant 计为多条)
COMPACT_SUMMARY_MAX_CHARS = 300      # 摘要长度约束(写进摘要指令)

# 上下文拼装(XML 标签式): <wxg_summary> 摘要 + <wxg_history> 最近K轮原文 + <wxg_current> 本轮消息, 注入 Web 输入框。
# 用标签闭合边界, 内容只需转义尖括号, 多行原样保留, 避免轮次被内容淹没。
CONTEXT_HEADER = (
    "以下 <wxg_summary> 是此前对话摘要, <wxg_history> 是最近若干轮原文(每个 <wxg_turn> 含 n=轮次、role=角色), "
    "<wxg_current> 是我当前的问题。请在此背景上继续回答, 不要复述背景本身。"
)

# 摘要指令模板({max_chars} / {conversation} 占位)
COMPACT_PROMPT_TEMPLATE = (
    "请把下面这段多轮对话压缩成一份简洁摘要, 只保留后续继续对话所必需的信息: "
    "关键事实、已达成的结论、尚未解决的问题、重要前提与用户偏好。"
    "用要点列出, 不要展开寒暄与客套, 不超过{max_chars}字。只输出摘要本身, 不要额外说明。\n\n"
    "====== 对话开始 ======\n{conversation}\n====== 对话结束 ======"
)


STEALTH_PATCH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true }); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'], configurable: true }); } catch (e) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { 0: { type: 'application/pdf' }, name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
        { 0: { type: 'application/pdf' }, name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 },
        { 0: { type: 'application/vnd.chromium.remoting-viewer' }, name: 'Chromoting Viewer', filename: 'internal-remoting-viewer', description: '', length: 1 },
        { 0: { type: 'application/x-pnacl' }, name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2 },
      ],
      configurable: true
    });
  } catch (e) {}
  try {
    if (window.chrome && !window.chrome.runtime) {
      Object.defineProperty(window.chrome, 'runtime', {
        value: {
          id: Math.random().toString(36).slice(2, 18),
          sendMessage: function() {},
          connect: function() { return { onMessage: { addListener: function(){} }, postMessage: function(){} }; },
          onMessage: { addListener: function(){}, removeListener: function(){} },
          onInstalled: { addListener: function(){}, removeListener: function(){} },
          lastError: undefined,
          PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
          PlatformArch: { ARM: 'arm', ARM64: 'arm64', X86_32: 'x86-32', X86_64: 'x86-64' },
          PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
          RequestUpdateCheckStatus: { THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available' },
          OnInstalledReason: { INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update', SHARED_MODULE_UPDATE: 'shared_module_update' },
        },
        configurable: true,
        writable: true
      });
    } else if (!window.chrome) {
      Object.defineProperty(window, 'chrome', {
        value: {
          runtime: { id: Math.random().toString(36).slice(2, 18), sendMessage: function(){} },
          app: {},
          loadTimes: function() { return {}; },
          csi: function() { return {}; }
        },
        configurable: true,
        writable: true
      });
    }
  } catch (e) {}
  try {
    const _orig = window.navigator.permissions.query;
    if (_orig) {
      window.navigator.permissions.query = function(params) {
        if (params && params.name === 'notifications') {
          return Promise.resolve({ state: Notification ? Notification.permission : 'granted', onchange: null });
        }
        return _orig.call(window.navigator.permissions, params);
      };
    }
  } catch (e) {}
  try {
    if (window.chrome && window.chrome.runtime && !Object.getOwnPropertyDescriptor(window.chrome.runtime, 'id')) {
      Object.defineProperty(window.chrome.runtime, 'id', { value: Math.random().toString(36).slice(2, 18), configurable: true });
    }
  } catch (e) {}
})();
"""

CAPTCHA_KEYWORDS = [
    "滑块验证", "滑动验证", "拖动滑块", "请按住滑块", "滑动上方滑块",
    "完成上方拼图", "旋转图片", "请完成验证", "人机验证", "行为验证",
    "验证失败", "安全验证", "请完成下方验证", "极验", "geetest",
    "请完成校验", "nc_iconfont", "滑动通过验证", "drag", "slide",
    "验证通过后即可", "需要验证", "淘宝", "captcha", "Captcha",
    "滑块", "验证条", "拼图", "缺口",
]

CAPTCHA_SELECTORS = [
    'iframe[src*="captcha"]',
    'iframe[src*="verify"]',
    'iframe[src*="security"]',
    'iframe[src*="validate"]',
    'div[class*="captcha"]',
    'div[class*="Captcha"]',
    'div[class*="slider"]',
    'div[class*="Slider"]',
    'div[class*="verify"]',
    'div[class*="Verify"]',
    'div[class*="geetest"]',
    'div[class*="nc-"]',
    'div[class*="slide-verify"]',
    'div[id*="captcha"]',
    'div[id*="nc_"]',
    'div[data-state="verify"]',
    'div[data-state="captcha"]',
]

LAST_CAPTCHA_ALERT = 0
CAPTCHA_ALERT_COOLDOWN = 8
CAPTCHA_DIAGNOSE = True  # 检测到滑块时自动截图 + 保存 DOM 信息(用于分析滑块结构)
# 滑块弹窗渲染等待: 轮询直到验证码 iframe 内真正渲染出手柄/提示 (替代固定 sleep)
CAPTCHA_RENDER_MAX_WAIT = 8.0   # 最长等待秒数
CAPTCHA_RENDER_POLL = 0.3       # 轮询间隔秒数
CAPTCHA_RENDER_SETTLE = 0.4     # 检测到已渲染后再稳定一小会儿, 确保完全绘制

# ---- 单轮回复的等待上限(防止验证拦截/静默失败时无限等待) ----
STREAM_OVERALL_TIMEOUT = 180    # 单轮(单次尝试)硬上限秒数: 无论如何超过即中止
CAPTCHA_RESTART_AFTER = 60      # (仅 API) 验证框持续这么久仍未完成 -> 重启标签页并自动重试
CAPTCHA_MAX_RESTARTS = 1        # (仅 API) 因验证未完成而重启标签页重试的最大次数
CAPTCHA_GONE_GRACE = 8          # 验证框消失后(手动关闭/验证失败), 仍无回复文字的宽限秒数, 超过判定被拦截并中止
FIRST_TOKEN_TIMEOUT = 60        # 无验证情况下, 等待首个回复 token 的上限秒数(兼容"思考研究"慢启动)

# ---- 浏览器内 (CDP/Playwright) 滑块定位/拖拽 ----
# 策略: CDP 视口截图 → 模板匹配(图像相似度)定位手柄 → Playwright page.mouse 拖拽
#       全程浏览器内, 不用 DOM、不碰 OS 鼠标/显示器。
PREFER_PLAYWRIGHT_SLIDE = True          # 优先走"图像+Playwright"路
USE_MIDSCENE_SLIDE_FALLBACK = False     # Midscene 暂时禁用(库的显示器映射有 bug); 后续需要再设 True
PLAYWRIGHT_SLIDE_MAX_RETRIES = 3        # 拖拽重试次数
# 手柄模板图(从实测弹窗裁出的 >> 按钮), 相对项目根目录
SLIDER_TEMPLATE_PATH = str(_PROJECT_ROOT / "slider_templates" / "handle.png")
SLIDER_TEMPLATE_THRESHOLD = 0.55        # matchTemplate 置信度阈值
SLIDER_LOCATE_MAX_WAIT = 8.0            # 轮询"截图+匹配"直到定位到手柄的最长秒数
SLIDER_LOCATE_POLL = 0.4                # 轮询间隔秒数


INPUT_SELECTORS = [
    'textarea[placeholder*="消息"]',
    'textarea[placeholder*="输入"]',
    'textarea[placeholder*="qwen" i]',
    'div[contenteditable="true"]',
    'textarea',
    '[role="textbox"]',
]

SEND_SELECTORS = [
    'button[aria-label*="发送"]',
    'button[aria-label*="Send"]',
    'button[type="submit"]',
    'button:has(svg)',
]

# "新建对话"按钮: 侧栏展开时是带文字的按钮, 收起时只剩左上角一个"加号"图标按钮,
# 二者是同一个"加号按钮"。以下按优先级兜底定位(优先无障碍属性, 再文字, 再图标结构)。
NEW_CHAT_SELECTORS = [
    'button[aria-label*="新建"]',
    'button[aria-label*="新对话"]',
    'button[aria-label*="New chat" i]',
    'button[title*="新建"]',
    'button[title*="新对话"]',
    'button:has-text("新建对话")',
    'a:has-text("新建对话")',
    '[role="button"]:has-text("新建对话")',
    # 收起态: 顶部工具区内含加号图标的按钮(结构兜底, 由 start_new_chat 进一步筛选)
    'button:has(svg)',
]

LOGIN_DETECTORS = [
    'input[placeholder*="手机"]', 'input[placeholder*="Phone"]', 'input[type="tel"]',
    'input[placeholder*="邮箱"]', 'input[placeholder*="Email"]', 'input[type="email"]',
    'text=/扫码/', 'text=/Scan/', 'text=/登录/', 'text=/Log in/', 'text=/Sign in/',
    'text=/验证码/', 'text=/获取验证码/',
    'button:has-text("登录")', 'button:has-text("Log in")', 'button:has-text("Sign in")',
]

MODEL_TRIGGER_SELECTORS = [
    '[aria-haspopup="dialog"][aria-controls^="radix-"]',
    '[aria-haspopup="dialog"]',
    '[aria-controls^="radix-"]',
    '[aria-expanded]',
    '.text-primary:has-text("Qwen")',
]

MODEL_PANEL_SELECTORS = [
    '[role="dialog"]',
    '[aria-labelledby]',
    '[class*="radix"] [class*="content"]',
    '[data-state="open"]',
]

MODEL_ITEM_SELECTORS = [
    # radix 标准菜单项(单层,主)
    '[role="menuitemcheckbox"]',
    '[role="menuitemradio"]',
    '[role="menuitem"]',
    '[data-radix-collection-item]',
    # 旧结构兜底
    'div[class*="truncate"][class*="text-14"]',
    'div[class*="truncate"]',
    '[role="dialog"] [role="option"]',
    '[role="dialog"] [role="menuitem"]',
    '[role="dialog"] button',
    '[role="dialog"] [class*="item"]',
    '[role="dialog"] [class*="model"]',
    '[class*="dialog"] [role="option"]',
    '[class*="popover"] [role="option"]',
    '[class*="content"] [role="option"]',
    '[class*="content"] [class*="item"]',
    '[class*="content"] button',
]

CHAT_MODE_TRIGGER_SELECTORS = [
    'button[aria-haspopup="dialog"]',
    'button:has-text("快速")',
    'button:has-text("思考")',
    'button:has-text("思考研究")',
    '[class*="chat-mode"] button',
    '[class*="ChatMode"] button',
    '[class*="mode-switch"] button',
]

CHAT_MODE_ITEM_SELECTORS = [
    # radix 标准菜单项(单层,主)
    '[role="menuitemcheckbox"]',
    '[role="menuitemradio"]',
    '[role="menuitem"]',
    '[data-radix-collection-item]',
    # 旧结构兜底
    '[role="dialog"] [role="option"]',
    '[role="dialog"] [role="menuitem"]',
    '[role="dialog"] button',
    '[class*="dialog"] [class*="item"]',
    '[class*="popover"] [class*="item"]',
    '[class*="content"] [class*="item"]',
    '[class*="content"] button',
]

DEFAULT_CHAT_MODE = ""
