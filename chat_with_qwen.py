import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
import socket
import uuid
import random
from pathlib import Path
from typing import AsyncGenerator, Optional
from aiohttp import web
from playwright.async_api import async_playwright

# macOS libedit 对 CJK 字符的退格有 bug, 用 gnureadline 替换
try:
    import gnureadline
    sys.modules['readline'] = gnureadline
except ImportError:
    pass

# 斜杠命令 TUI 面板（输入 / 弹出命令菜单）
from cmd_palette import prompt_query, print_help, normalize_command

# 交互式命令注册表: 输入 / 触发 TUI 菜单, 命令名 -> 说明
COMMANDS = {
    "/help":      "查看所有命令列表",
    "/models":    "查看可用模型列表",
    "/model":     "切换模型, 用法: /model <模型名>",
    "/chatmodes": "查看对话模式列表",
    "/chatmode":  "切换对话模式, 用法: /chatmode <模式名>",
    "/quit":      "退出程序",
}


# ============ 配置区 ============
CHROME_PATH = r"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEBUG_PORT = 9223
USER_DATA_DIR = Path("./qwen_chrome_profile")
QWEN_URL = "https://www.qianwen.com/"
QWEN_HOST = "qianwen.com"
HEADLESS = False
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

# ============ API 配置 ============
API_HOST = "0.0.0.0"
API_PORT = 8765
API_KEY = ""  # 留空则不鉴权
API_MODEL = "qwen-web"

# ============ 全局状态 ============
_app_state = {
    "page": None,
    "browser": None,
    "context": None,
    "lock": None,
    "round_num": 0,
}

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
# ===============================


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S") + f",{int(time.time() * 1000) % 1000:03d}"
    print(f"[执行日志] {ts} {msg}", flush=True)


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


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_chrome_debug_port(port: int) -> bool:
    """通过 HTTP 请求 /json/version 验证端口是否为有效的 Chrome DevTools 端点"""
    import urllib.request
    try:
        url = f"http://127.0.0.1:{port}/json/version"
        req = urllib.request.Request(url, headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "webSocketDebuggerUrl" in data
    except Exception:
        return False


def get_chrome_tabs(port: int) -> list:
    """获取 Chrome 的标签页列表，返回 [{"type":..., "title":..., "url":...}]"""
    import urllib.request
    try:
        url = f"http://127.0.0.1:{port}/json/list"
        req = urllib.request.Request(url, headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        return []


def is_chrome_usable(port: int) -> bool:
    """验证端口是否为有效的 Chrome DevTools 端点且至少有一个标签页
    （窗口被关闭但进程残留时，DevTools 端点可能仍响应，但标签页为 0，
    此时 Playwright connect_over_cdp 会报 Browser context management is not supported）
    """
    if not is_chrome_debug_port(port):
        return False
    tabs = get_chrome_tabs(port)
    return len(tabs) > 0


def find_port_processes(port: int):
    """返回占用端口的进程列表 [(pid, cmd)]"""
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], stderr=subprocess.DEVNULL
        ).decode().strip()
        if not out:
            return []
        results = []
        for pid in out.split("\n"):
            pid = pid.strip()
            if not pid:
                continue
            try:
                cmd_out = subprocess.check_output(
                    ["ps", "-p", pid, "-o", "comm="], stderr=subprocess.DEVNULL
                ).decode().strip()
                results.append((pid, cmd_out))
            except Exception:
                results.append((pid, ""))
        return results
    except Exception:
        return []


def kill_processes(pids):
    for pid in pids:
        try:
            subprocess.run(["kill", pid], check=False, stderr=subprocess.DEVNULL)
            log(f"   已发送 kill 信号到 PID {pid}")
        except Exception:
            pass


def launch_chrome():
    USER_DATA_DIR.mkdir(exist_ok=True)

    if is_chrome_usable(DEBUG_PORT):
        log(f"✅ 检测到 Chrome DevTools 端口 {DEBUG_PORT} 可用且有标签页 → 直接附加")
        return None

    if is_chrome_debug_port(DEBUG_PORT):
        tabs = get_chrome_tabs(DEBUG_PORT)
        log(f"⚠️ Chrome DevTools 端点响应但无标签页（tabs={len(tabs)}），Chrome 窗口可能已关闭")
        procs = find_port_processes(DEBUG_PORT)
        chrome_pids = [pid for pid, cmd in procs if "chrome" in cmd.lower()]
        if chrome_pids:
            log(f"🔧 检测到 Chrome 残留进程，尝试清理并重启...")
            kill_processes(chrome_pids)
            time.sleep(2)
            if is_port_open(DEBUG_PORT):
                log(f"⚠️ 清理后端口仍被占用，等待 3 秒后再检测...")
                time.sleep(3)
            if is_port_open(DEBUG_PORT):
                log(f"❌ 端口 {DEBUG_PORT} 无法释放，请手动处理")
                raise RuntimeError(f"端口 {DEBUG_PORT} 被占用且无法释放")
        else:
            log(f"⚠️ 未找到 Chrome 进程，端口可能处于 TIME_WAIT，等待 3 秒...")
            time.sleep(3)

    elif is_port_open(DEBUG_PORT):
        procs = find_port_processes(DEBUG_PORT)
        if procs:
            log(f"⚠️ 端口 {DEBUG_PORT} 被占用，但不是有效的 Chrome DevTools 端点:")
            for pid, cmd in procs:
                log(f"   PID {pid}: {cmd}")

            chrome_pids = [pid for pid, cmd in procs if "chrome" in cmd.lower()]
            if chrome_pids:
                log(f"🔧 检测到 Chrome 残留进程，尝试清理...")
                kill_processes(chrome_pids)
                time.sleep(2)
                if is_port_open(DEBUG_PORT):
                    log(f"⚠️ 清理后端口仍被占用，等待 3 秒后再检测...")
                    time.sleep(3)
                if is_port_open(DEBUG_PORT):
                    log(f"❌ 端口 {DEBUG_PORT} 无法释放，请手动处理")
                    raise RuntimeError(f"端口 {DEBUG_PORT} 被占用且无法释放")
            else:
                log(f"❌ 端口被非 Chrome 进程占用，请手动释放后重试")
                raise RuntimeError(f"端口 {DEBUG_PORT} 被非 Chrome 进程占用")
        else:
            log(f"⚠️ 端口 {DEBUG_PORT} 处于 TIME_WAIT 状态，等待 3 秒...")
            time.sleep(3)
            if is_port_open(DEBUG_PORT):
                log(f"❌ 端口仍被占用")
                raise RuntimeError(f"端口 {DEBUG_PORT} 处于不可用状态")

    log(f"🚀 启动 Chrome (port={DEBUG_PORT}) ...")
    cmd = [CHROME_PATH, f"--remote-debugging-port={DEBUG_PORT}",
           f"--user-data-dir={USER_DATA_DIR.resolve()}",
           "--no-first-run", "--no-default-browser-check"]
    if HEADLESS:
        cmd.append("--headless=new")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if is_port_open(DEBUG_PORT):
            log("Chrome 调试端口已就绪")
            return proc
        time.sleep(0.2)
    raise RuntimeError("Chrome 启动超时")


async def find_element(page, selectors, description):
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1500):
                log(f"找到{description}: {sel}")
                return el
        except Exception:
            continue
    return None


async def find_or_create_qwen_page(context):
    if REUSE_EXISTING_TAB:
        for pg in context.pages:
            try:
                if QWEN_HOST in pg.url:
                    log(f"♻️  复用已有标签页: {pg.url}")
                    try:
                        await pg.bring_to_front()
                    except Exception:
                        pass
                    return pg, True
            except Exception:
                continue
    page = await context.new_page()
    return page, False


async def is_on_login_page(page) -> bool:
    for sel in LOGIN_DETECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


async def detect_captcha(page):
    global LAST_CAPTCHA_ALERT
    visible_hits = []
    for sel in CAPTCHA_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=100):
                visible_hits.append(sel)
                break
        except Exception:
            continue
    try:
        kw_hit = await page.evaluate("""() => {
            const kws = [%s];
            const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
            let count = 0;
            let node;
            const sample = [];
            while ((node = walker.nextNode()) && count < 2000) {
                const t = node.textContent.trim();
                if (t && t.length < 50) {
                    for (const k of kws) {
                        if (t.includes(k)) {
                            if (sample.length < 3) sample.push(t);
                            count++;
                            break;
                        }
                    }
                }
            }
            return { count, sample };
        }()""" % (",".join([f'"{k}"' for k in CAPTCHA_KEYWORDS])))
        kw_count = kw_hit.get("count", 0) if isinstance(kw_hit, dict) else 0
    except Exception:
        kw_count = 0
    found = bool(visible_hits or kw_count >= 2)
    if found:
        now = time.time()
        if now - LAST_CAPTCHA_ALERT > CAPTCHA_ALERT_COOLDOWN:
            LAST_CAPTCHA_ALERT = now
            detail = []
            if visible_hits:
                detail.append(f"元素: {visible_hits[:3]}")
            log("\n" + "=" * 60)
            log("⚠️  检测到滑块/人机验证！")
            if detail:
                log(f"   详情: {', '.join(detail)}")

            # ---- 滑块诊断: 截图 + DOM 信息 ----
            if CAPTCHA_DIAGNOSE:
                await _captcha_diagnose(page, visible_hits)

            # ---- 自动尝试滑动 ----
            auto_success = False
            if AUTO_SLIDE_CAPTCHA:
                auto_success = await _try_auto_slide_captcha(page)

            if not auto_success:
                log("   请在浏览器中手动完成验证")
                log("   （划动滑块或完成验证后，脚本会自动继续）")
                log("=" * 60 + "\n")
            else:
                log("=" * 60 + "\n")

            # 如果自动滑动成功, 返回 False 表示 captcha 已解决
            if auto_success:
                return False

    return found


async def _captcha_diagnose(page, visible_selectors):
    """检测到滑块后, 等待渲染完成, 截图并保存 DOM 结构信息, 用于分析滑块位置和样式"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    RAW_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    viewport_png = RAW_DUMP_DIR / f"captcha_{ts}_viewport.png"
    fullpage_png = RAW_DUMP_DIR / f"captcha_{ts}_fullpage.png"
    info_json = RAW_DUMP_DIR / f"captcha_{ts}_info.json"

    try:
        # 等待滑块完全渲染(避免半渲染状态)
        await asyncio.sleep(0.8)

        # 1. 视口截图(与 page.mouse 坐标系一致, 用于后续视觉定位)
        await page.screenshot(path=str(viewport_png), full_page=False)
        log(f"📸 [诊断] 视口截图已保存: {viewport_png}")

        # 2. 全页截图(参考用)
        await page.screenshot(path=str(fullpage_png), full_page=True)
        log(f"📸 [诊断] 全页截图已保存: {fullpage_png}")

        # 3. 获取视口 CSS 像素尺寸(连接已有 Chrome 时 viewport_size 可能为 None, 用 JS 兜底)
        vp_info = {"width": 0, "height": 0}
        try:
            vp = page.viewport_size
            if vp:
                vp_info = {"width": vp["width"], "height": vp["height"]}
            else:
                # 通过 JS 获取
                js_vp = await page.evaluate("""() => ({
                    width: window.innerWidth || document.documentElement.clientWidth || 0,
                    height: window.innerHeight || document.documentElement.clientHeight || 0,
                    dpr: window.devicePixelRatio || 1,
                })""")
                vp_info = {
                    "width": js_vp.get("width", 0),
                    "height": js_vp.get("height", 0),
                    "dpr": js_vp.get("dpr", 1),
                }
        except Exception:
            pass

        # 4. 收集 DOM 信息: 遍历所有 frame
        frames_info = []
        for i, frame in enumerate(page.frames):
            try:
                frame_info = {
                    "index": i,
                    "url": frame.url[:200] if frame.url else "(main)",
                }
                try:
                    # 在每个 frame 内查找候选滑块元素
                    candidates = await frame.evaluate("""(selectors) => {
                        const results = [];
                        for (const sel of selectors) {
                            try {
                                const els = document.querySelectorAll(sel);
                                for (const el of els) {
                                    if (!el.getBoundingClientRect) continue;
                                    const r = el.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {
                                        results.push({
                                            selector: sel,
                                            bbox: {x: Math.round(r.x), y: Math.round(r.y),
                                                   w: Math.round(r.width), h: Math.round(r.height)},
                                            text: (el.textContent || '').trim().substring(0, 60),
                                            tag: el.tagName,
                                            class: (el.className || '').toString().substring(0, 100),
                                        });
                                    }
                                }
                            } catch(e) {}
                        }
                        return results;
                    }""", CAPTCHA_SELECTORS, timeout=2000)
                    if candidates:
                        frame_info["candidates"] = candidates
                        frame_info["candidate_count"] = len(candidates)
                except Exception as e:
                    frame_info["error"] = str(e)[:100]
                frames_info.append(frame_info)
            except Exception as e:
                frames_info.append({"index": i, "error": str(e)[:100]})

        # 5. 主页面通用属性查找(不含 iframe 内的)
        extra_candidates = []
        try:
            extra_candidates = await page.evaluate("""() => {
                const results = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (!el.getBoundingClientRect) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width < 30 || r.width > 500) continue;
                    if (r.height < 10 || r.height > 100) continue;
                    const cls = (el.className || '').toString().toLowerCase();
                    const id = (el.id || '').toLowerCase();
                    const dataAttrs = Object.values(el.dataset || {}).join(' ').toLowerCase();
                    const haystack = (cls + ' ' + id + ' ' + dataAttrs);
                    if (haystack.includes('captcha') || haystack.includes('slider') ||
                        haystack.includes('verify') || haystack.includes('nc-') ||
                        haystack.includes('geetest') || haystack.includes('slide')) {
                        results.push({
                            tag: el.tagName,
                            class: (el.className || '').toString().substring(0, 80),
                            id: (el.id || '')[:60],
                            bbox: {x: Math.round(r.x), y: Math.round(r.y),
                                   w: Math.round(r.width), h: Math.round(r.height)},
                            text: (el.textContent || '').trim().substring(0, 40),
                        });
                    }
                }
                return results.slice(0, 20);
            }""", timeout=3000)
        except Exception:
            pass

        # 6. 汇总信息并保存
        total_candidates = sum(f.get("candidate_count", 0) for f in frames_info)
        info = {
            "captcha_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "viewport": vp_info,
            "visible_selectors": visible_selectors,
            "frames": frames_info,
            "extra_candidates": extra_candidates,
            "total_frames": len(page.frames),
            "total_candidates_in_frames": total_candidates,
        }
        info_json.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"📋 [诊断] DOM 信息已保存: {info_json}")
        log(f"   CSS视口 {vp_info.get('width',0)}x{vp_info.get('height',0)} (DPR={vp_info.get('dpr',1)})")
        log(f"   {len(page.frames)} 个iframe, frame内{total_candidates}个候选, 主页面{len(extra_candidates)}个候选")

    except Exception as e:
        log(f"⚠️ [诊断] 截图/信息收集异常: {e}")


# ============ 滑块自动滑动: 视觉定位 + 人类化拖动 ============

AUTO_SLIDE_CAPTCHA = True  # 自动尝试滑动滑块(失败后回退到手动模式)
SLIDE_MAX_RETRIES = 3      # 自动滑动最大重试次数

async def _locate_slider_from_screenshot(screenshot_path: str, dpr: float = 1.0):
    """从截图中定位滑块的手柄和轨道位置, 返回 CSS 像素坐标
    返回 None 表示定位失败"""
    try:
        import cv2
        import numpy as np

        img = cv2.imread(screenshot_path)
        if img is None:
            log("   [debug] 截图读取失败")
            return None
        h, w = img.shape[:2]
        scale = 1.0 / dpr if dpr > 0 else 1.0
        log(f"   [debug] 截图尺寸: {w}x{h}, scale={scale:.3f}")

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        # 1. 先找白色/浅色弹窗
        lower_light = np.array([0, 0, 180])
        upper_light = np.array([180, 50, 255])
        light_mask = cv2.inRange(hsv, lower_light, upper_light)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 20))
        light_closed = cv2.morphologyEx(light_mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(light_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        popup = None
        found_popups = []
        for cnt in sorted(contours, key=cv2.contourArea, reverse=True):
            area = cv2.contourArea(cnt)
            if area > 20000:
                x, y, cw, ch = cv2.boundingRect(cnt)
                center_x = x + cw/2
                found_popups.append((x, y, cw, ch, area))
                log(f"   [debug] 发现浅色区域: 位置({x},{y}), 尺寸{cw}x{ch}, 面积{area:.0f}, 中心({center_x:.0f},{y+ch/2:.0f})")
                if abs(center_x - w/2) < w/3 and popup is None:
                    popup = (x, y, cw, ch)
                    log(f"   [debug] 选定弹窗区域: {popup}")

        if popup is None:
            log("   [debug] 未找到弹窗, 使用默认中心区域")
            cx, cy = w // 2, h // 2
            popup = (cx - 250, cy - 200, 500, 400)

        px, py, pw, ph = popup

        # 2. 裁剪弹窗区域
        roi_x1 = max(0, px + 20)
        roi_y1 = max(0, py + ph // 2)
        roi_x2 = min(w, px + pw - 20)
        roi_y2 = min(h, py + ph - 30)
        roi = img[roi_y1:roi_y2, roi_x1:roi_x2]

        log(f"   [debug] 搜索区域: ({roi_x1},{roi_y1})-({roi_x2},{roi_y2}), 尺寸{roi.shape[1]}x{roi.shape[0]}")

        if roi.size == 0:
            log("   [debug] ROI 为空")
            return None

        roi_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        # 3. 用边缘检测找矩形滑块轨道
        edges = cv2.Canny(roi_gray, 20, 80)
        kernel_h = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 1))
        edges_dilated = cv2.dilate(edges, kernel_h, iterations=2)
        edges_closed = cv2.morphologyEx(edges_dilated, cv2.MORPH_CLOSE, kernel_h)

        track_contours, _ = cv2.findContours(edges_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        log(f"   [debug] 边缘检测找到 {len(track_contours)} 个轮廓")

        best_track = None
        track_candidates = []
        for cnt in track_contours:
            area = cv2.contourArea(cnt)
            if area < 100:
                continue
            x, y, cw, ch = cv2.boundingRect(cnt)
            aspect = cw / max(ch, 1)
            if cw > 150 and 10 <= ch <= 60 and aspect > 6:
                track_candidates.append((roi_x1 + x, roi_y1 + y, cw, ch, area, aspect))
                log(f"   [debug] 候选轨道: 位置({roi_x1+x},{roi_y1+y}), 尺寸{cw}x{ch}, 宽高比{aspect:.1f}, 面积{area:.0f}")
                if best_track is None or cw * ch > best_track[2] * best_track[3]:
                    best_track = (roi_x1 + x, roi_y1 + y, cw, ch)

        # 备选: 颜色检测
        if best_track is None:
            log("   [debug] 边缘检测未找到轨道, 尝试颜色检测...")
            roi_hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            lower_gray = np.array([0, 0, 150])
            upper_gray = np.array([180, 40, 230])
            gray_mask = cv2.inRange(roi_hsv, lower_gray, upper_gray)
            gray_closed = cv2.morphologyEx(gray_mask, cv2.MORPH_CLOSE, kernel)
            contours2, _ = cv2.findContours(gray_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            for cnt in contours2:
                area = cv2.contourArea(cnt)
                if area < 500:
                    continue
                x, y, cw, ch = cv2.boundingRect(cnt)
                aspect = cw / max(ch, 1)
                if cw > 150 and 10 <= ch <= 60 and aspect > 5:
                    log(f"   [debug] 颜色检测候选轨道: 位置({roi_x1+x},{roi_y1+y}), 尺寸{cw}x{ch}, 宽高比{aspect:.1f}")
                    if best_track is None or cw * ch > best_track[2] * best_track[3]:
                        best_track = (roi_x1 + x, roi_y1 + y, cw, ch)

        if best_track is None:
            log("   [debug] 未找到滑块轨道")
            return None

        track_x, track_y, track_w, track_h = best_track
        log(f"   [debug] 选定轨道: ({track_x},{track_y}), 宽{track_w}, 高{track_h}")

        # 4. 找滑块手柄
        handle_search_x1 = max(0, track_x - 10)
        handle_search_y1 = max(0, track_y - 20)
        handle_search_x2 = min(w, track_x + track_w + 10)
        handle_search_y2 = min(h, track_y + track_h + 20)
        handle_roi = img[handle_search_y1:handle_search_y2, handle_search_x1:handle_search_x2]

        handle_hsv = cv2.cvtColor(handle_roi, cv2.COLOR_BGR2HSV)
        handle_gray = cv2.cvtColor(handle_roi, cv2.COLOR_BGR2GRAY)

        lower_white = np.array([0, 0, 200])
        upper_white = np.array([180, 60, 255])
        white_mask = cv2.inRange(handle_hsv, lower_white, upper_white)
        _, bright_mask = cv2.threshold(handle_gray, 200, 255, cv2.THRESH_BINARY)
        combined_mask = cv2.bitwise_or(white_mask, bright_mask)

        handle_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        combined_closed = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, handle_kernel)
        handle_contours, _ = cv2.findContours(combined_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        track_cy = track_y + track_h / 2
        best_handle = None

        log(f"   [debug] 手柄搜索区域: ({handle_search_x1},{handle_search_y1})-({handle_search_x2},{handle_search_y2})")
        log(f"   [debug] 找到 {len(handle_contours)} 个候选手柄轮廓")

        for cnt in handle_contours:
            area = cv2.contourArea(cnt)
            if area < 100 or area > 5000:
                continue
            hx, hy, hw, hh = cv2.boundingRect(cnt)
            handle_cx = handle_search_x1 + hx + hw / 2
            handle_cy = handle_search_y1 + hy + hh / 2
            cy_diff = abs(handle_cy - track_cy)
            if cy_diff < track_h / 2 + 15:
                log(f"   [debug] 候选手柄: 位置({handle_cx:.0f},{handle_cy:.0f}), 尺寸{hw}x{hh}, 面积{area:.0f}, Y差{cy_diff:.0f}")
                if best_handle is None or area > best_handle[4]:
                    best_handle = (handle_search_x1 + hx, handle_search_y1 + hy, hw, hh, area)

        if best_handle is None:
            log("   [debug] 未找到滑块手柄")
            return None

        hx, hy, hw, hh, _ = best_handle
        handle_cx = hx + hw / 2
        handle_cy = hy + hh / 2

        result = {
            "handle_x": handle_cx * scale,
            "handle_y": handle_cy * scale,
            "track_width": track_w * scale,
            "track_height": track_h * scale,
            "handle_width": hw * scale,
            "handle_height": hh * scale,
            "total_distance": (track_w - hw) * scale,
            "screenshot_size": {"width": w, "height": h},
            "css_size": {"width": int(w * scale), "height": int(h * scale)},
        }
        log(f"   [debug] 定位成功: 手柄({result['handle_x']:.0f},{result['handle_y']:.0f}), 轨道宽{result['track_width']:.0f}, 移动距离{result['total_distance']:.0f}")
        return result

    except Exception as e:
        log(f"   [debug] 定位异常: {e}")
        return None


# ============ Midscene OS 级客户端 ============

async def _midscene_health_check() -> bool:
    """检查 Midscene 服务是否可用"""
    if not MIDSCENE_ENABLED:
        return False
    try:
        import urllib.request
        url = f"{MIDSCENE_BASE_URL}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode())
            return data.get("status") == "ok"
    except Exception:
        return False


async def _midscene_locate_slider(prompt: str = "") -> Optional[dict]:
    """
    调用 Midscene 视觉定位滑块
    返回: {"handle": {"x", "y"}, "gap": {"x", "y"}} 或 None
    """
    if not MIDSCENE_ENABLED:
        return None
    try:
        import urllib.request
        payload = json.dumps({"prompt": prompt}).encode()
        req = urllib.request.Request(
            f"{MIDSCENE_BASE_URL}/locate_slider",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            if data.get("success"):
                log(f"   [Midscene] 视觉定位成功: handle={data['handle']}, gap={data.get('gap')}")
                return {"handle": data["handle"], "gap": data.get("gap")}
            else:
                log(f"   [Midscene] 视觉定位失败: {data.get('error')}")
                return None
    except Exception as e:
        log(f"   [Midscene] 调用异常: {e}")
        return None


async def _midscene_perform_drag(points: list, start_delay_ms: int = 0, end_delay_ms: int = 0) -> bool:
    """
    调用 Midscene OS 级拖拽
    points: [{"x", "y", "delayMs"}, ...]
    """
    if not MIDSCENE_ENABLED:
        return False
    try:
        import urllib.request
        payload = json.dumps({
            "points": points,
            "startDelayMs": start_delay_ms,
            "endDelayMs": end_delay_ms
        }).encode()
        req = urllib.request.Request(
            f"{MIDSCENE_BASE_URL}/perform_drag",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            return data.get("success", False)
    except Exception as e:
        log(f"   [Midscene] 拖拽调用异常: {e}")
        return False


def _build_drag_trajectory(handle_x: float, handle_y: float, distance: float, 
                           screen_offset_x: float = 0, screen_offset_y: float = 0) -> list:
    """
    构建物理仿真拖拽轨迹(复用现有 _human_drag_slider 的算法)
    返回 Midscene 需要的 points 数组
    
    screen_offset: 浏览器窗口左上角在屏幕上的偏移量(macOS 需要)
    """
    total_distance = distance
    screen_x = handle_x + screen_offset_x
    screen_y = handle_y + screen_offset_y
    
    points = []
    
    # 1. 起点(带随机偏移)
    offset_x = random.uniform(-3, 3)
    offset_y = random.uniform(-3, 3)
    target_x = screen_x + offset_x
    target_y = screen_y + offset_y
    
    # 起点
    points.append({"x": target_x - random.uniform(5, 15), "y": target_y, "delayMs": 0})
    points.append({"x": target_x, "y": target_y, "delayMs": 50})
    
    # 2. 加速 -> 匀速 -> 减速
    current_x = target_x
    current_y = target_y
    steps_count = max(int(total_distance / random.uniform(4, 8)), 20)
    
    for i in range(steps_count):
        t = i / steps_count
        velocity = 1.0 - abs(2 * t - 1) ** 2  # 钟形速度曲线
        step_distance = (total_distance / steps_count) * (0.5 + velocity * 0.8)
        step_distance += random.uniform(-0.5, 0.5)
        y_drift = random.uniform(-1.5, 1.5)
        
        current_x += step_distance
        current_y = screen_y + y_drift
        
        points.append({
            "x": current_x,
            "y": current_y,
            "delayMs": int(8 + random.uniform(-3, 5))
        })
    
    # 3. 过冲
    overshoot = random.uniform(2, 4)
    points.append({
        "x": current_x + overshoot,
        "y": current_y + random.uniform(-1, 1),
        "delayMs": int(30 + random.uniform(0, 50))
    })
    
    # 4. 回调
    points.append({
        "x": current_x - overshoot / 2,
        "y": current_y + random.uniform(-1, 1),
        "delayMs": int(50 + random.uniform(0, 50))
    })
    
    # 5. 最终位置
    final_x = screen_x + total_distance
    final_y = screen_y + random.uniform(-1, 1)
    points.append({"x": final_x, "y": final_y, "delayMs": int(150 + random.uniform(0, 150))})
    
    return points


async def _human_drag_slider_os(page, handle_x: float, handle_y: float, distance: float):
    """
    使用 Midscene OS 级拖拽滑块(路线 A)
    需要 Midscene Node.js 服务运行中
    """
    if not MIDSCENE_ENABLED:
        log("   [Midscene] 未启用,回退到 Playwright 拖拽")
        return await _human_drag_slider(page, handle_x, handle_y, distance)
    
    # 1. 获取浏览器窗口在屏幕上的位置(OS 级坐标需要)
    try:
        win_info = await page.evaluate("""() => ({
            screenX: window.screenX || window.screenLeft || 0,
            screenY: window.screenY || window.screenTop || 0,
            outerWidth: window.outerWidth,
            outerHeight: window.outerHeight,
            innerWidth: window.innerWidth,
            innerHeight: window.innerHeight,
            dpr: window.devicePixelRatio || 1,
        })""")
        # macOS Chrome: window.screenX/Y 表示窗口左上角的屏幕坐标
        screen_offset_x = win_info.get("screenX", 0) + (win_info.get("outerWidth", 0) - win_info.get("innerWidth", 0)) // 2
        screen_offset_y = win_info.get("screenY", 0) + (win_info.get("outerHeight", 0) - win_info.get("innerHeight", 0)) - 1
        dpr = win_info.get("dpr", 1)
        log(f"   [Midscene] 窗口偏移: ({screen_offset_x}, {screen_offset_y}), DPR={dpr}")
    except Exception as e:
        log(f"   [Midscene] 获取窗口位置失败: {e}, 使用估算值")
        screen_offset_x = 0
        screen_offset_y = 0
    
    # 2. 等待 Midscene 服务
    if not await _midscene_health_check():
        log("   [Midscene] 服务不可用,回退到 Playwright 拖拽")
        return await _human_drag_slider(page, handle_x, handle_y, distance)
    
    # 3. 构建轨迹(CSS 坐标 + 窗口偏移 = 屏幕坐标)
    points = _build_drag_trajectory(handle_x, handle_y, distance, screen_offset_x, screen_offset_y)
    log(f"   [Midscene] 轨迹: {len(points)} 个点")
    
    # 4. OS 级拖拽
    # 按下前停顿:模拟"鼠标移到滑块 → 思考 → 按下"
    start_delay = int(120 + random.uniform(0, 200))
    # 松开后停顿:模拟"确认验证结果"
    end_delay = int(150 + random.uniform(0, 200))
    
    success = await _midscene_perform_drag(points, start_delay, end_delay)
    
    if success:
        log(f"   [Midscene] OS 级拖拽执行完成")
    else:
        log(f"   [Midscene] OS 级拖拽失败,回退到 Playwright")
        return await _human_drag_slider(page, handle_x, handle_y, distance)
    
    return success


async def _human_drag_slider(page, handle_x: float, handle_y: float, distance: float):
    """用 page.mouse 人类化地拖动滑块, 返回是否成功"""
    total_distance = distance  # 需要拖动的总距离( CSS 像素)

    # 1. 移动到滑块位置(带随机偏移)
    offset_x = random.uniform(-3, 3)
    offset_y = random.uniform(-3, 3)
    target_x = handle_x + offset_x
    target_y = handle_y + offset_y

    # 先快速移到附近, 再微调
    await page.mouse.move(target_x - random.uniform(5, 15), target_y, steps=10)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.move(target_x, target_y, steps=5)
    await asyncio.sleep(random.uniform(0.2, 0.4))  # 反应时间

    # 2. 按下鼠标
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.1, 0.2))  # 按下后短暂停顿

    # 3. 分段拖动: 加速 -> 匀速 -> 减速
    current_x = target_x
    current_y = target_y
    steps_count = max(int(total_distance / random.uniform(4, 8)), 20)
    time_per_step = 0.008  # 每步 8ms

    for i in range(steps_count):
        t = i / steps_count
        # 速度曲线: 钟形(sin 曲线), 两端慢, 中间快
        velocity = 1.0 - abs(2 * t - 1) ** 2  # 0~1~0 的钟形
        step_distance = (total_distance / steps_count) * (0.5 + velocity * 0.8)
        step_distance += random.uniform(-0.5, 0.5)  # 微小抖动

        # Y 轴随机漂移(模拟手的不稳定)
        y_drift = random.uniform(-1.5, 1.5)

        current_x += step_distance
        current_y += y_drift

        await page.mouse.move(current_x, current_y, steps=1)
        await asyncio.sleep(time_per_step + random.uniform(-0.003, 0.005))

    # 4. 过冲: 稍微拖过一点再拉回(模拟真人过冲回调)
    overshoot = random.uniform(2, 4)
    await page.mouse.move(current_x + overshoot, current_y + random.uniform(-1, 1), steps=3)
    await asyncio.sleep(random.uniform(0.03, 0.08))

    # 回调
    await page.mouse.move(current_x - overshoot / 2, current_y + random.uniform(-1, 1), steps=3)
    await asyncio.sleep(random.uniform(0.05, 0.1))

    # 最终位置
    final_x = handle_x + total_distance
    final_y = handle_y + random.uniform(-1, 1)
    await page.mouse.move(final_x, final_y, steps=5)
    await asyncio.sleep(random.uniform(0.15, 0.3))

    # 5. 抬起鼠标
    await page.mouse.up()
    await asyncio.sleep(random.uniform(0.2, 0.4))

    return True


async def _try_auto_slide_captcha(page):
    """尝试自动滑动滑块, 返回是否成功"""
    if not AUTO_SLIDE_CAPTCHA:
        return False

    log("🤖 尝试自动滑动滑块...")

    # 1. 获取 DPR 和 CSS 视口尺寸
    try:
        info = await page.evaluate("""() => ({
            dpr: window.devicePixelRatio || 1,
            innerW: window.innerWidth || 0,
            innerH: window.innerHeight || 0,
        })""")
        dpr = info.get("dpr", 1)
        css_w = info.get("innerW", 0)
        css_h = info.get("innerH", 0)
    except Exception:
        dpr = 1
        css_w, css_h = 1280, 800

    # 2. 截图用于视觉定位
    ts = time.strftime("%Y%m%d_%H%M%S")
    RAW_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = str(RAW_DUMP_DIR / f"captcha_auto_{ts}.png")
    try:
        await page.screenshot(path=screenshot_path, full_page=False)
        log(f"   截图: {screenshot_path}")
    except Exception:
        log("   ⚠️ 截图失败")
        return False

    # 3. 视觉定位(优先 Midscene, 回退 OpenCV)
    loc = None
    
    # 3a. 尝试 Midscene OS 级视觉定位
    if MIDSCENE_ENABLED and await _midscene_health_check():
        log(f"   [Midscene] 尝试 AI 视觉定位...")
        midscene_result = await _midscene_locate_slider(
            '屏幕上有一个滑块验证区域,请找到滑块手柄的位置和目标缺口位置。滑块通常在屏幕下方或弹窗内。'
        )
        if midscene_result and midscene_result.get("handle"):
            h = midscene_result["handle"]
            g = midscene_result.get("gap")
            # Midscene 返回的是屏幕坐标,转换为 CSS 坐标(需要减去窗口偏移)
            # 简化处理:假设 Chrome 最大化或我们已知偏移
            loc = {
                "handle_x": h["x"] - 0,  # 精确偏移需要结合窗口位置计算
                "handle_y": h["y"] - 0,
                "total_distance": abs(g["x"] - h["x"]) if g else 200,
                "track_width": abs(g["x"] - h["x"]) if g else 300,
            }
            log(f"   [Midscene] 定位结果: handle=({h['x']:.0f},{h['y']:.0f}), gap=({g['x']:.0f},{g['y']:.0f})"
                f", 距离={loc['total_distance']:.0f}")
        else:
            log(f"   [Midscene] AI 定位失败, 回退到 OpenCV")
    
    # 3b. 回退: OpenCV 截图定位
    if loc is None:
        log(f"   分析截图(DPR={dpr}, CSS视口{css_w}x{css_h})...")
        loc = await _locate_slider_from_screenshot(screenshot_path, dpr)
        if loc is None:
            log("   ⚠️ 视觉定位失败, 回退到手动模式")
            return False

        log(f"   定位结果: 手柄({loc['handle_x']:.0f},{loc['handle_y']:.0f})"
            f", 轨道宽{loc['track_width']:.0f}, 需拖动{loc['total_distance']:.0f}像素")

    # 4. 自动拖动(优先 Midscene OS 级, 回退 Playwright)
    for attempt in range(SLIDE_MAX_RETRIES):
        log(f"   尝试 {attempt + 1}/{SLIDE_MAX_RETRIES}...")
        try:
            if MIDSCENE_ENABLED and await _midscene_health_check():
                # Midscene OS 级拖拽(路线 A)
                success = await _human_drag_slider_os(
                    page,
                    loc["handle_x"],
                    loc["handle_y"],
                    loc["total_distance"]
                )
            else:
                # Playwright 拖拽(原有方式)
                success = await _human_drag_slider(
                    page,
                    loc["handle_x"],
                    loc["handle_y"],
                    loc["total_distance"]
                )

            # 等待验证结果
            await asyncio.sleep(1.0)

            # 检查滑块是否还存在(如果成功验证, 滑块应该消失)
            still_visible = False
            for sel in CAPTCHA_SELECTORS:
                try:
                    locator = page.locator(sel).first
                    if await locator.count() > 0 and await locator.is_visible(timeout=200):
                        still_visible = True
                        break
                except Exception:
                    pass

            if not still_visible:
                log(f"   ✅ 自动滑动成功! (第{attempt + 1}次尝试)")
                return True
            elif not success:
                log(f"   ⚠️ 拖拽执行失败, 重试...")
                await asyncio.sleep(0.3)
            else:
                log(f"   ⚠️ 滑块仍然可见, 重试...")
                await asyncio.sleep(0.3)
                # 重新截图定位(可能滑块位置变了)
                if attempt < SLIDE_MAX_RETRIES - 1:
                    await page.screenshot(path=screenshot_path, full_page=False)
                    new_loc = await _locate_slider_from_screenshot(screenshot_path, dpr)
                    if new_loc:
                        loc = new_loc
                        log(f"   重新定位: 需拖动{loc['total_distance']:.0f}像素")

        except Exception as e:
            log(f"   ⚠️ 拖动异常: {e}")
            await asyncio.sleep(0.5)

    log(f"   ❌ 自动滑动失败({SLIDE_MAX_RETRIES}次尝试), 请手动完成")
    return False


_last_mouse_jitter = 0
def _human_keystroke_delay_ms(prev_char: str, curr_char: str) -> int:
    import random, math
    # 对数正态分布：中位数 ~53ms, σ=0.30
    base = random.lognormvariate(math.log(0.053), 0.30) * 1000
    # 标点前减速（当前字符是标点）
    if curr_char in "，。！？；：、,.!?;:":
        base *= 1.6
    # 句末长停顿（前一个字符是句末标点）
    if prev_char in "。！？!?":
        base += random.uniform(200, 550)
    # 生理下限 35ms
    return max(int(base), 35)

async def mouse_jitter(page):
    global _last_mouse_jitter
    import random
    now = time.time()
    if now - _last_mouse_jitter < 3.5:
        return
    _last_mouse_jitter = now
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        cx = random.randint(int(viewport["width"] * 0.15), int(viewport["width"] * 0.85))
        cy = random.randint(int(viewport["height"] * 0.2), int(viewport["height"] * 0.8))
        steps = random.randint(3, 6)
        await page.mouse.move(cx, cy, steps=steps)
    except Exception:
        pass


async def wait_for_login_then_chat(page, timeout_sec):
    log("检测页面状态...")
    deadline = time.time() + timeout_sec
    login_hinted = False
    while time.time() < deadline:
        if await find_element(page, INPUT_SELECTORS, "对话输入框"):
            log("✅ 登录已完成")
            return True
        if await is_on_login_page(page):
            if not login_hinted:
                log("=" * 60)
                log("⚠️  请手动登录！登录后脚本自动继续")
                log(f"⏳ 最长等待 {timeout_sec}s")
                log("=" * 60)
                login_hinted = True
        await page.wait_for_timeout(3000)
    return False


async def ensure_modes(page, mode_names):
    log(f"🎛️  检查模式: {mode_names}")
    check_js = """(el) => {
        if (!el) return false;
        const p = el.getAttribute('aria-pressed');
        const c = el.getAttribute('aria-checked');
        if (p === 'true' || c === 'true') return true;
        const cls = ((el.className||'')+' '+(el.parentElement&&el.parentElement.className||'')).toLowerCase();
        return ['active','selected','checked','-on','enable','primary'].some(k=>cls.includes(k));
    }"""
    for name in mode_names:
        try:
            chip = None
            for sel in [f'button:has-text("{name}")', f'[role="button"]:has-text("{name}")',
                        f'div[role="switch"]:has-text("{name}")', f'span:has-text("{name}")']:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                        chip = loc
                        break
                except Exception:
                    continue
            if chip:
                handle = await chip.element_handle()
                if await page.evaluate(check_js, handle):
                    log(f"   ✅ 「{name}」已开启")
                else:
                    log(f"   🔘 开启「{name}」...")
                    await chip.click()
                    await page.wait_for_timeout(800)
                    h2 = await chip.element_handle()
                    on = await page.evaluate(check_js, h2)
                    log(f"   {'✅' if on else '⚠️'} 「{name}」→ {'已开启' if on else '可能未开启'}")
            else:
                log(f"   ⚠️ 未找到「{name}」独立按钮，尝试从对话模式下拉菜单切换...")
                if await switch_chat_mode(page, name):
                    log(f"   ✅ 「{name}」通过对话模式切换成功")
                else:
                    log(f"   ❌ 「{name}」在页面中未找到")
        except Exception as e:
            log(f"   ❌ 「{name}」出错: {e}")
    log("🎛️  模式设置完成")


MODEL_BLACKLIST_KEYWORDS = [
    "办公助理", "PPT", "AI生成", "AI作图", "AI图", "本地电脑",
    "更多", "联网搜索", "深度搜索", "深度研究", "思考研究",
    "快速", "扫码", "下载", "注册", "登录",
]


def _is_valid_model_name(name: str) -> bool:
    if not name or len(name) > 60:
        return False
    for kw in MODEL_BLACKLIST_KEYWORDS:
        if kw in name:
            return False
    return True


MODEL_NAME_PATTERN = re.compile(r"^Qwen", re.IGNORECASE)


async def _validate_model_trigger(locator):
    try:
        full_text = (await locator.inner_text(timeout=1000)).strip()
        log(f"      [validate] full_text=「{full_text}」")
        if MODEL_NAME_PATTERN.match(full_text):
            return True
    except Exception as e:
        log(f"      [validate] inner_text异常: {e}")

    try:
        inner = locator.locator(".text-primary").first
        cnt = await inner.count()
        log(f"      [validate] .text-primary count={cnt}")
        if cnt > 0:
            text = (await inner.inner_text(timeout=1000)).strip()
            log(f"      [validate] .text-primary text=「{text}」")
            if MODEL_NAME_PATTERN.match(text):
                return True
    except Exception as e:
        log(f"      [validate] .text-primary异常: {e}")

    return False


async def _validate_chat_mode_trigger(locator):
    try:
        text = (await locator.inner_text(timeout=1000)).strip()
        log(f"      [validate_chat_mode] text=「{text}」")
        mode_keywords = ["快速", "思考", "思考研究"]
        for kw in mode_keywords:
            if kw in text:
                return True
    except Exception as e:
        log(f"      [validate_chat_mode] inner_text异常: {e}")
    return False


async def _diagnose_page_structure(page):
    log("   🔍 诊断页面结构...")
    try:
        diag_js = """() => {
            const results = {};
            results.totalButtons = document.querySelectorAll('button').length;
            results.typeButtons = document.querySelectorAll('button[type="button"]').length;
            results.hasPopupDialog = document.querySelectorAll('[aria-haspopup="dialog"]').length;
            results.hasPopupDialogButton = document.querySelectorAll('button[aria-haspopup="dialog"]').length;
            results.hasPopupDialogTypeButton = document.querySelectorAll('button[type="button"][aria-haspopup="dialog"]').length;
            results.hasControlsRadix = document.querySelectorAll('[aria-controls^="radix"]').length;
            results.hasControlsRadixButton = document.querySelectorAll('button[aria-controls^="radix"]').length;
            results.hasTextPrimary = document.querySelectorAll('.text-primary').length;
            results.qwenTextPrimary = Array.from(document.querySelectorAll('.text-primary'))
                .filter(el => el.textContent.trim().startsWith('Qwen')).length;
            results.textPrimarySamples = Array.from(document.querySelectorAll('.text-primary'))
                .slice(0, 10).map(el => el.textContent.trim());
            
            const iframes = document.querySelectorAll('iframe');
            results.iframeCount = iframes.length;
            results.iframeDetails = Array.from(iframes).map(f => ({
                src: f.src ? f.src.substring(0, 100) : '(no src)',
                id: f.id || '',
                className: f.className || ''
            }));
            
            results.bodyClasses = document.body ? document.body.className.substring(0, 200) : '';
            
            return results;
        }"""
        result = await page.evaluate(diag_js)
        log(f"     button 总数: {result.get('totalButtons', 'N/A')}")
        log(f"     type=button: {result.get('typeButtons', 'N/A')}")
        log(f"     aria-haspopup=dialog: {result.get('hasPopupDialog', 'N/A')}")
        log(f"     button+aria-haspopup=dialog: {result.get('hasPopupDialogButton', 'N/A')}")
        log(f"     button[type=button]+aria-haspopup=dialog: {result.get('hasPopupDialogTypeButton', 'N/A')}")
        log(f"     aria-controls^=radix: {result.get('hasControlsRadix', 'N/A')}")
        log(f"     button+aria-controls^=radix: {result.get('hasControlsRadixButton', 'N/A')}")
        log(f"     .text-primary: {result.get('hasTextPrimary', 'N/A')}")
        log(f"     .text-primary 以Qwen开头: {result.get('qwenTextPrimary', 'N/A')}")
        log(f"     .text-primary 样例: {result.get('textPrimarySamples', [])}")
        log(f"     iframe 数量: {result.get('iframeCount', 'N/A')}")
        if result.get('iframeDetails'):
            for d in result.get('iframeDetails', []):
                log(f"       iframe: id={d.get('id')} class={d.get('className')} src={d.get('src', '')[:80]}")
        return result
    except Exception as e:
        log(f"     诊断异常: {e}")
        return {}


async def _open_and_get_panel(page, trigger_selectors, panel_selectors, validate_fn=None):
    trigger = None
    for sel in trigger_selectors:
        try:
            loc = page.locator(sel).first
            cnt = await loc.count()
            log(f"   [selector] {sel} → count={cnt}")
            if cnt > 0:
                visible = await loc.is_visible(timeout=2000)
                log(f"   [selector] visible={visible}")
                if not visible:
                    continue
                if validate_fn:
                    passed = await validate_fn(loc)
                    log(f"   [selector] validate={passed}")
                    if not passed:
                        continue
                text = ""
                try:
                    text = await loc.inner_text(timeout=500)
                except Exception:
                    pass
                controls_id = await loc.get_attribute("aria-controls") or ""
                log(f"   触发器匹配: {sel} → 文本:「{text.strip()}」 aria-controls={controls_id}")
                trigger = loc
                break
        except Exception as e:
            log(f"   [selector] {sel} → 异常: {e}")
            continue
    if not trigger:
        await _diagnose_page_structure(page)
        return None, None

    expanded_before = await trigger.get_attribute("aria-expanded") or "false"
    log(f"   点击前 aria-expanded={expanded_before}")

    if expanded_before == "true":
        log("   面板已展开，跳过点击")
    else:
        # 点击前的认知停顿(模拟"找到按钮→移动鼠标→决定点击")
        await page.wait_for_timeout(random.randint(120, 320))
        log("   尝试点击打开面板...")
        clicked = False
        for attempt in range(3):
            try:
                await trigger.click()
                clicked = True
                break
            except Exception:
                try:
                    await trigger.evaluate("el => el.click()")
                    clicked = True
                    break
                except Exception:
                    await page.wait_for_timeout(random.randint(400, 800))
        
        if not clicked:
            log("   ❌ 无法点击按钮")
            return None, None

    # 点击后面板展开的等待(动画 + 渲染)
    await page.wait_for_timeout(random.randint(400, 800))

    expanded_after_click = await trigger.get_attribute("aria-expanded") or "false"
    log(f"   点击后 aria-expanded={expanded_after_click}")

    if expanded_after_click != "true":
        log("   面板未展开，等待后重试...")
        await page.wait_for_timeout(random.randint(600, 1000))
        expanded_after_click = await trigger.get_attribute("aria-expanded") or "false"
        log(f"   重试后 aria-expanded={expanded_after_click}")

    panel = None
    controls_id = await trigger.get_attribute("aria-controls") or ""
    log(f"   aria-controls={controls_id}")

    if controls_id:
        try:
            escaped_id = controls_id.replace(":", "\\:")
            loc = page.locator(f'#{escaped_id}').first
            if await loc.count() > 0:
                visible = await loc.is_visible(timeout=2000)
                log(f"   #{controls_id} 可见={visible}")
                if visible:
                    inner_len = await loc.evaluate("el => el.innerHTML.length")
                    log(f"   #{controls_id} innerHTML长度={inner_len}")
                    if inner_len > 100:
                        log(f"   面板匹配: #{controls_id}")
                        panel = loc
                    else:
                        log(f"   #{controls_id} 内容过少，可能是占位符")
        except Exception as e:
            log(f"   #{controls_id} 定位异常: {e}")

    if not panel:
        for psel in panel_selectors:
            try:
                loc = page.locator(psel).first
                if await loc.count() > 0 and await loc.is_visible(timeout=2000):
                    inner_len = await loc.evaluate("el => el.innerHTML.length")
                    log(f"   面板匹配: {psel} innerHTML长度={inner_len}")
                    if inner_len > 100:
                        panel = loc
                        break
            except Exception:
                continue

    if not panel and controls_id:
        try:
            loc = page.locator(f'#{controls_id}').first
            if await loc.count() > 0:
                log(f"   面板匹配(无转义): #{controls_id}")
                panel = loc
        except Exception:
            pass

    if not panel:
        try:
            loc = page.locator('[data-state="open"]').first
            if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                inner_len = await loc.evaluate("el => el.innerHTML.length")
                log(f"   [data-state=open] innerHTML长度={inner_len}")
                if inner_len > 100:
                    log(f"   面板匹配: [data-state=open]")
                    panel = loc
        except Exception:
            pass

    if not panel:
        try:
            all_dialogs = page.locator('[role="dialog"]')
            for i in range(await all_dialogs.count()):
                d = all_dialogs.nth(i)
                if await d.is_visible(timeout=500):
                    inner_len = await d.evaluate("el => el.innerHTML.length")
                    log(f"   [role=dialog] #{i} innerHTML长度={inner_len}")
                    if inner_len > 100:
                        log(f"   面板匹配: [role=dialog] #{i}")
                        panel = d
                        break
        except Exception:
            pass

    if not panel:
        log("   ⚠️ 未能定位展开的面板")
        return trigger, None

    return trigger, panel


async def _extract_items_from_panel(page, panel, item_selectors, blacklist_fn):
    results = []
    seen = set()
    for sel in item_selectors:
        try:
            items = panel.locator(sel)
            count = await items.count()
            if count > 0:
                log(f"   面板内选择器命中 {count} 个: {sel}")
                for i in range(count):
                    try:
                        text = (await items.nth(i).inner_text(timeout=1000)).strip()
                        if not text:
                            continue
                        text = _clean_model_name(text)
                        if text in seen:
                            continue
                        if blacklist_fn(text):
                            seen.add(text)
                            results.append(text)
                        else:
                            log(f"   过滤掉: 「{text}」")
                    except Exception:
                        continue
                if results:
                    break
                else:
                    log(f"   选择器 {sel} 全部被过滤，尝试下一个...")
        except Exception:
            continue

    if not results:
        log("   Playwright选择器未命中，改用JS直接提取菜单项...")
        try:
            js_result = await panel.evaluate("""el => {
                const itemEls = el.querySelectorAll(
                    '[role="menuitemcheckbox"], [role="menuitemradio"], [role="menuitem"], [data-radix-collection-item]'
                );
                const items = [];
                const seen = new Set();
                for (const it of itemEls) {
                    let primary = null;
                    const primarySpans = it.querySelectorAll(
                        '[class*="text-primary"]:not([class*="text-caption"])'
                    );
                    for (const s of primarySpans) {
                        const t = s.textContent.trim();
                        if (t) { primary = t; break; }
                    }
                    if (!primary) {
                        const full = (it.innerText || it.textContent || '').trim();
                        primary = full.split('\\n')[0].trim();
                    }
                    if (primary && primary.length >= 2 && primary.length < 30 && !seen.has(primary)) {
                        seen.add(primary);
                        items.push(primary);
                    }
                }
                return { items, total: itemEls.length };
            }""")
            log(f"   JS匹配到 {len(js_result.get('items', []))} 个(菜单项总数={js_result.get('total')}):")
            for name in js_result.get("items", []):
                if blacklist_fn(name):
                    results.append(name)
                else:
                    log(f"   过滤掉: 「{name}」")
        except Exception as e:
            log(f"   JS提取异常: {e}")

    return results


def _clean_model_name(text: str) -> str:
    text = text.split('\n')[0].strip()
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'(新模型|默认|设为默认|推荐)$', '', text).strip()
    text = re.sub(r'[，,].*$', '', text).strip()
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


async def list_models(page):
    log("📋 获取模型列表...")
    trigger, panel = await _open_and_get_panel(page, MODEL_TRIGGER_SELECTORS, MODEL_PANEL_SELECTORS, validate_fn=_validate_model_trigger)

    if not trigger:
        log("   ⚠️ 未找到模型切换按钮")
        return []
    if not panel:
        log("   ⚠️ 面板未展开")
        return []

    models = await _extract_items_from_panel(page, panel, MODEL_ITEM_SELECTORS, _is_valid_model_name)

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    if models:
        log(f"   发现 {len(models)} 个模型:")
        for m in models:
            log(f"   - {m}")
    else:
        log("   ⚠️ 未能获取模型列表")
    return models


async def switch_model(page, model_name):
    log(f"🔄 切换模型 → 「{model_name}」")
    trigger, panel = await _open_and_get_panel(page, MODEL_TRIGGER_SELECTORS, MODEL_PANEL_SELECTORS, validate_fn=_validate_model_trigger)

    if not trigger:
        log("   ❌ 未找到模型切换按钮")
        return False
    if not panel:
        log("   ❌ 面板未展开")
        return False

    try:
        exact = panel.locator(f'div[class*="truncate"]:has-text("{model_name}")').first
        if await exact.count() > 0 and await exact.is_visible(timeout=2000):
            await exact.click()
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        exact2 = panel.get_by_text(model_name, exact=True).first
        if await exact2.count() > 0 and await exact2.is_visible(timeout=2000):
            await exact2.click()
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        fuzzy = panel.get_by_text(model_name, exact=False).first
        if await fuzzy.count() > 0 and await fuzzy.is_visible(timeout=2000):
            await fuzzy.click()
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        fallback_js = f"""el => {{
            if (!el) return false;
            const selectors = ['div[class*="truncate"]', '[role="option"]', '[role="menuitem"]', 'button', '[class*="item"]'];
            for (const sel of selectors) {{
                const els = el.querySelectorAll(sel);
                for (const el2 of els) {{
                    const t = el2.textContent.trim();
                    const cleaned = t.replace(/\\s+/g, ' ').replace(/(新模型|默认|设为默认)$/g, '').trim();
                    if (cleaned === '{model_name}' || cleaned.includes('{model_name}')) {{
                        el2.click();
                        return true;
                    }}
                }}
            }}
            return false;
        }}"""
        result = await panel.evaluate(fallback_js)
        if result:
            await page.wait_for_timeout(500)
            log(f"   ✅ 已切换到「{model_name}」")
            return True
    except Exception:
        pass

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    log(f"   ❌ 未找到模型「{model_name}」")
    return False


def _is_valid_chat_mode_name(name: str) -> bool:
    if not name or len(name) > 60:
        return False
    blacklist = ["办公助理", "PPT", "AI生成", "AI作图", "AI图", "本地电脑", "更多", "扫码", "下载", "注册", "登录"]
    for kw in blacklist:
        if kw in name:
            return False
    return True


async def list_chat_modes(page):
    log("📋 获取对话模式列表...")
    trigger, panel = await _open_and_get_panel(page, CHAT_MODE_TRIGGER_SELECTORS, MODEL_PANEL_SELECTORS, validate_fn=_validate_chat_mode_trigger)

    if not trigger:
        log("   ⚠️ 未找到对话模式切换按钮")
        return []
    if not panel:
        log("   ⚠️ 面板未展开")
        return []

    modes = await _extract_items_from_panel(page, panel, CHAT_MODE_ITEM_SELECTORS, _is_valid_chat_mode_name)

    # 关闭面板前停顿(模拟"看完列表→决定关闭"的认知过程)
    await page.wait_for_timeout(random.randint(250, 550))
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    # 关闭动画完成
    await page.wait_for_timeout(random.randint(300, 600))

    if modes:
        log(f"   发现 {len(modes)} 个对话模式:")
        for m in modes:
            log(f"   - {m}")
    else:
        log("   ⚠️ 未能获取对话模式列表")
    return modes


async def switch_chat_mode(page, mode_name):
    log(f"🔄 切换对话模式 → 「{mode_name}」")
    trigger, panel = await _open_and_get_panel(page, CHAT_MODE_TRIGGER_SELECTORS, MODEL_PANEL_SELECTORS, validate_fn=_validate_chat_mode_trigger)

    if not trigger:
        log("   ❌ 未找到对话模式切换按钮")
        return False
    if not panel:
        log("   ❌ 面板未展开")
        return False

    # 读取选项前的视觉停顿(模拟"扫一眼列表→定位目标")
    await page.wait_for_timeout(random.randint(180, 450))

    async def _try_click_option(loc) -> bool:
        """尝试点击一个选项,带人类化时序"""
        if await loc.count() > 0 and await loc.is_visible(timeout=2000):
            # 点击前的认知停顿(模拟"决定点击这个")
            await page.wait_for_timeout(random.randint(150, 400))
            await loc.click()
            # 切换模式涉及 UI 变化,等待更长
            await page.wait_for_timeout(random.randint(600, 1200))
            return True
        return False

    # 1. 精确选择器(radix 菜单项)
    try:
        exact = panel.locator(f'[role="menuitemcheckbox"]:has-text("{mode_name}")').first
        if await _try_click_option(exact):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 2. 其他菜单项角色
    try:
        exact_alt = panel.locator(f'[role="menuitem"]:has-text("{mode_name}"), [role="menuitemradio"]:has-text("{mode_name}")').first
        if await _try_click_option(exact_alt):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 3. 精确文本匹配
    try:
        exact2 = panel.get_by_text(mode_name, exact=True).first
        if await _try_click_option(exact2):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 4. 模糊文本匹配
    try:
        fuzzy = panel.get_by_text(mode_name, exact=False).first
        if await _try_click_option(fuzzy):
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 5. JS fallback(更新选择器,优先 menuitemcheckbox)
    try:
        fallback_js = f"""(panelEl) => {{
            if (!panelEl) return false;
            const selectors = [
                '[role="menuitemcheckbox"]', '[role="menuitemradio"]',
                '[role="menuitem"]', '[data-radix-collection-item]',
                '[role="option"]', 'button', '[class*="item"]'
            ];
            for (const sel of selectors) {{
                const els = panelEl.querySelectorAll(sel);
                for (const el of els) {{
                    const t = (el.textContent || '').trim();
                    if (t === '{mode_name}' || t.startsWith('{mode_name}')) {{
                        el.click();
                        return true;
                    }}
                }}
            }}
            return false;
        }}"""
        result = await page.evaluate(fallback_js, panel)
        if result:
            await page.wait_for_timeout(random.randint(600, 1200))
            log(f"   ✅ 已切换到「{mode_name}」")
            return True
    except Exception:
        pass

    # 关闭面板前停顿
    await page.wait_for_timeout(random.randint(200, 450))
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(random.randint(300, 600))
    log(f"   ❌ 未找到对话模式「{mode_name}」")
    return False


# ============================================================
# Hook JS —— 针对千问 SSE 格式
# SSE 结构: event:message / event:complete + data:{json}
# JSON: data.messages[].mime_type 分流:
#   multi_load/iframe   → 正文(累积式，提取增量)
#                        + 同时检查 meta_data.multi_load[].content.think_content
#                          (思考过程, 累积式提取增量, 路由到 __dsThink)
#   plan_cot/post       → 思考状态信号(status: processing/complete, content 为空, 静默)
#   bar/progress        → 进度提示(仅记录，不混入思考)
#   bar/iframe          → 搜索来源(忽略)
#   signal/post         → 控制信号(忽略)
# 其他未识别 mime_type   → 通过 __dsDebug 上报(诊断用)
# 原始 SSE 行           → 通过 __dsRawSse 上报(诊断用, 仅当 DUMP_RAW_SSE)
# 结束信号: event:complete 或 data.status==="complete"
# ============================================================
HOOK_JS_QWEN = r"""
() => {
    const HOOK_VERSION = 'v3';
    if (window.__ds_hook_qwen === HOOK_VERSION) return 'already';

    // 检测是否存在旧版 hook 残留
    const hadOldHook = window.__ds_hook_qwen === true || (window.__ds_hook_qwen && typeof window.__ds_hook_qwen === 'string' && window.__ds_hook_qwen !== HOOK_VERSION);

    // 首次注入时保存原始 fetch/XHR 引用, 后续版本升级重新注入时基于原始引用, 避免嵌套
    if (!window.__ds_orig_fetch) {
        window.__ds_orig_fetch = window.fetch.bind(window);
    }
    if (!window.__ds_orig_xhr) {
        window.__ds_orig_xhr = window.XMLHttpRequest;
    }

    window.__ds_hook_qwen = HOOK_VERSION;
    window.__ds_finished = false;
    window.__ds_lastTextLen = 0;
    window.__ds_hookSource = '';

    function makeProcessor() {
        let cursor_text = '';
        let lastBodyText = '';
        let lastThinkText = '';
        let lastProgressText = '';
        let currentEvent = '';
        let done = false;
        const seenMimeTypes = {};

        function emitThinkDelta(content) {
            if (!content || content.length === 0) return;
            // 累积式: 新内容以旧内容为前缀时, 只发增量
            if (lastThinkText === '' || content.startsWith(lastThinkText)) {
                if (content.length > lastThinkText.length) {
                    const delta = content.substring(lastThinkText.length);
                    lastThinkText = content;
                    try { window.__dsThink(delta); } catch(e){}
                }
            } else {
                // 非累积(内容被替换): 全量发送
                lastThinkText = content;
                try { window.__dsThink(content); } catch(e){}
            }
        }

        function handleMessage(obj) {
            const data = obj.data;
            if (!data) return;
            const messages = data.messages;
            if (!Array.isArray(messages)) return;
            const dataStatus = data.status || '';

            if (obj.error_code && obj.error_code !== 0) {
                try { window.__dsThink('⚠️ 错误: ' + (obj.error_msg || 'unknown')); } catch(e){}
            }

            for (const msg of messages) {
                const mt = msg.mime_type || '';
                const content = msg.content || '';

                // 统计 mime_type 出现次数(诊断用)
                if (mt) {
                    seenMimeTypes[mt] = (seenMimeTypes[mt] || 0) + 1;
                }

                if (mt === 'multi_load/iframe') {
                    // 正文: 累积式，提取增量
                    if (content && content.length > 0) {
                        if (lastBodyText === '' || content.startsWith(lastBodyText)) {
                            if (content.length > lastBodyText.length) {
                                const delta = content.substring(lastBodyText.length);
                                lastBodyText = content;
                                try { window.__dsChunk(delta); } catch(e){}
                            }
                        } else {
                            lastBodyText = content;
                            try { window.__dsChunk(content); } catch(e){}
                        }
                    }
                    // ★ 关键: 从 meta_data.multi_load 提取 think_content (思考过程)
                    // 结构: meta_data.multi_load[].content.think_content (累积式)
                    const meta = msg.meta_data;
                    if (meta && Array.isArray(meta.multi_load)) {
                        for (const item of meta.multi_load) {
                            if (item && item.type === 'multimodal_chat_think') {
                                const c = item.content;
                                if (c && typeof c.think_content === 'string') {
                                    emitThinkDelta(c.think_content);
                                }
                            }
                        }
                    }
                } else if (mt === 'plan_cot/post') {
                    // 思考状态信号(status: processing/complete, content 为空), 静默忽略
                } else if (mt === 'bar/progress') {
                    // 进度条: 仅 debug 记录, 不混入思考内容
                    if (content && content !== lastProgressText) {
                        lastProgressText = content;
                        try { window.__dsDebug('progress|' + content); } catch(e){}
                    }
                } else if (mt === 'bar/iframe' || mt === 'signal/post' || mt === 'signal/complete') {
                    // 已知控制/搜索来源信号, 静默忽略
                } else {
                    // 未识别 mime_type: 上报给 Python 端用于诊断
                    try {
                        const preview = (content || '').substring(0, 80);
                        window.__dsDebug('unknown_mt|' + mt + '|' + preview);
                    } catch(e){}
                }
            }

            if (dataStatus === 'complete' || currentEvent === 'complete') {
                if (!done) {
                    done = true;
                    window.__ds_finished = true;
                    // 把 mime_type 统计作为最后一条 debug 上报
                    try {
                        const summary = Object.keys(seenMimeTypes)
                            .map(k => k + '=' + seenMimeTypes[k])
                            .join(',');
                        window.__dsDebug('mime_summary|' + summary);
                    } catch(e){}
                    try { window.__dsDone(); } catch(e){}
                }
            }
        }

        function processLine(line) {
            // 原始 SSE 行上报(诊断用)
            try { window.__dsRawSse(line + '\n'); } catch(e){}

            const trimmed = line.trim();
            if (!trimmed) return;

            if (trimmed.startsWith('event:')) {
                currentEvent = trimmed.substring(6).trim();
                return;
            }

            if (!trimmed.startsWith('data:')) return;
            const payload = trimmed.substring(5).trim();
            if (!payload) return;

            let obj;
            try { obj = JSON.parse(payload); } catch(e) { return; }
            handleMessage(obj);
        }

        function processChunk(text) {
            if (done) return;
            cursor_text += text;
            window.__ds_lastTextLen = (window.__ds_lastTextLen || 0) + text.length;

            const nl = cursor_text.lastIndexOf('\n');
            if (nl === -1) return;
            const complete = cursor_text.substring(0, nl + 1);
            cursor_text = cursor_text.substring(nl + 1);

            const lines = complete.split('\n');
            // 去掉末尾空串(由最后一个 \n 产生)
            for (let i = 0; i < lines.length - 1; i++) {
                processLine(lines[i]);
            }
        }

        function forceFinish() {
            if (done) return;
            if (cursor_text.trim()) {
                const remaining = cursor_text;
                cursor_text = '';
                const lines = remaining.split('\n');
                for (const line of lines) {
                    processLine(line);
                }
            }
            done = true;
            if (!window.__ds_finished) {
                window.__ds_finished = true;
            }
            try { window.__dsDone(); } catch(e){}
        }

        return { processChunk, forceFinish };
    }

    // ========== Hook XMLHttpRequest ==========
    const OrigXHR = window.__ds_orig_xhr;
    window.XMLHttpRequest = function(...args) {
        const xhr = new OrigXHR(...args);
        const origOpen = xhr.open.bind(xhr);
        const origSend = xhr.send.bind(xhr);

        xhr.open = function(method, url, ...rest) {
            this.__ds_url = (typeof url === 'string') ? url : '';
            return origOpen(method, url, ...rest);
        };

        xhr.send = function(...a) {
            if (this.__ds_url && this.__ds_url.includes('/api/v2/chat')) {
                window.__ds_hookSource = 'xhr';
                const proc = makeProcessor();
                let lastLen = 0;

                this.addEventListener('progress', () => {
                    const full = this.responseText || '';
                    if (full.length > lastLen) {
                        proc.processChunk(full.substring(lastLen));
                        lastLen = full.length;
                    }
                });

                this.addEventListener('readystatechange', () => {
                    const full = this.responseText || '';
                    if (full.length > lastLen) {
                        proc.processChunk(full.substring(lastLen));
                        lastLen = full.length;
                    }
                    if (this.readyState === 4) {
                        proc.forceFinish();
                    }
                });

                this.addEventListener('loadend', () => {
                    const full = this.responseText || '';
                    if (full.length > lastLen) {
                        proc.processChunk(full.substring(lastLen));
                        lastLen = full.length;
                    }
                    proc.forceFinish();
                });
            }
            return origSend(...a);
        };

        return xhr;
    };
    window.XMLHttpRequest.prototype = OrigXHR.prototype;
    for (const k of ['UNSENT','OPENED','HEADERS_RECEIVED','LOADING','DONE']) {
        if (k in OrigXHR) window.XMLHttpRequest[k] = OrigXHR[k];
    }

    // ========== Hook fetch ==========
    const origFetch = window.__ds_orig_fetch;
    window.fetch = function(...args) {
        const url = (typeof args[0] === 'string') ? args[0] :
                    (args[0] && args[0].url) ? args[0].url : '';

        if (url.includes('/api/v2/chat')) {
            window.__ds_hookSource = 'fetch';
            const proc = makeProcessor();

            return origFetch.apply(this, args).then(response => {
                const cloned = response.clone();
                (async () => {
                    try {
                        const reader = cloned.body.getReader();
                        const decoder = new TextDecoder();
                        while (true) {
                            const { done, value } = await reader.read();
                            if (done) {
                                proc.forceFinish();
                                break;
                            }
                            const text = decoder.decode(value, { stream: true });
                            proc.processChunk(text);
                        }
                    } catch(e) {
                        proc.forceFinish();
                    }
                })();
                return response;
            });
        }

        return origFetch.apply(this, args);
    };

    return hadOldHook ? 'hooked_v3_overlay' : 'hooked_v3_clean';
}
"""

POLL_STATE_JS = """() => ({
    finished: !!window.__ds_finished,
    textLen: window.__ds_lastTextLen || 0,
    source: window.__ds_hookSource || ''
})"""


async def stream_chat(page, send_coro, round_num, idle_timeout=15):
    cdp = await page.context.new_cdp_session(page)
    chunk_queue = asyncio.Queue()
    live_parts = []
    think_parts = []
    raw_sse_parts = []
    debug_lines = []
    stream_prefix_printed = False
    think_prefix_printed = False
    chunk_count = 0
    think_count = 0
    finished_by_signal = False

    await cdp.send("Runtime.enable")
    await cdp.send("Runtime.addBinding", {"name": "__dsChunk"})
    await cdp.send("Runtime.addBinding", {"name": "__dsThink"})
    await cdp.send("Runtime.addBinding", {"name": "__dsDone"})
    await cdp.send("Runtime.addBinding", {"name": "__dsRawSse"})
    await cdp.send("Runtime.addBinding", {"name": "__dsDebug"})

    def on_binding(params):
        name = params.get("name", "")
        payload = params.get("payload", "")
        if name == "__dsChunk":
            chunk_queue.put_nowait(("body", payload))
        elif name == "__dsThink":
            chunk_queue.put_nowait(("think", payload))
        elif name == "__dsDone":
            chunk_queue.put_nowait(("done", None))
        elif name == "__dsRawSse":
            # 原始 SSE 行, 直接收集, 不走主队列(避免阻塞流式输出)
            if DUMP_RAW_SSE and payload:
                raw_sse_parts.append(payload)
        elif name == "__dsDebug":
            # 调试信息, 收集并实时打日志
            if payload:
                debug_lines.append(payload)
                # 实时输出 debug(截断长内容)
                log(f"🐛 [debug] {payload[:200]}")

    cdp.on("Runtime.bindingCalled", on_binding)

    hook_result = await page.evaluate(HOOK_JS_QWEN)
    await page.evaluate("""
        window.__ds_finished = false;
        window.__ds_lastTextLen = 0;
        window.__ds_hookSource = '';
    """)
    log(f"Hook: {hook_result}")
    if hook_result and hook_result.startswith('hooked_v3_overlay'):
        log("❌ 检测到旧版 Hook 残留(Chrome 进程未重启)")
        log("   旧版与新版 Hook 会同时调用同一个 binding, 导致正文重复 + 提前结束")
        log("   请关闭 Chrome 进程后重新运行脚本, 全新注入 v3 Hook")
        log("   (本次对话终止, 不会产生有效输出)")
        return 0, 0, ""
    elif hook_result == 'already':
        log("Hook 版本匹配, 复用现有注入")

    try:
        await send_coro

        if STREAM_OUTPUT:
            log("开始接收流式数据...")

        last_active = time.time()
        last_text_len = 0
        hook_source_logged = False

        while True:
            try:
                kind, data = await asyncio.wait_for(chunk_queue.get(), timeout=0.2)
                last_active = time.time()

                if kind == "done":
                    finished_by_signal = True
                    while not chunk_queue.empty():
                        try:
                            k2, d2 = chunk_queue.get_nowait()
                            if k2 == "body" and d2:
                                # 剥离正文中的 [(multimodal_chat_think_N)] 引用标签
                                body_data = d2
                                if STRIP_THINK_REF_TAGS:
                                    body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                                live_parts.append(body_data)
                                if think_prefix_printed:
                                    print("\033[0m")  # 重置颜色
                                    think_prefix_printed = False
                                if not stream_prefix_printed:
                                    print("AI: ", end="", flush=True)
                                    stream_prefix_printed = True
                                print(body_data, end="", flush=True)
                            elif k2 == "think" and d2:
                                think_parts.append(d2)
                                if STREAM_OUTPUT:
                                    if not think_prefix_printed:
                                        print("\033[90m💭 ", end="", flush=True)
                                        think_prefix_printed = True
                                    print(d2, end="", flush=True)
                        except asyncio.QueueEmpty:
                            break
                    break

                elif kind == "think":
                    think_count += 1
                    think_parts.append(data)
                    if STREAM_OUTPUT and data:
                        if not think_prefix_printed:
                            print("\033[90m💭 ", end="", flush=True)
                            think_prefix_printed = True
                        print(data, end="", flush=True)

                elif kind == "body":
                    chunk_count += 1
                    if data:
                        # 剥离正文中的 [(multimodal_chat_think_N)] 引用标签
                        body_data = data
                        if STRIP_THINK_REF_TAGS:
                            body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                        if think_prefix_printed:
                            # 思考→正文切换: 重置颜色 + 换行
                            print("\033[0m")
                            think_prefix_printed = False
                        if not stream_prefix_printed:
                            print("AI: ", end="", flush=True)
                            stream_prefix_printed = True
                        print(body_data, end="", flush=True)
                        live_parts.append(body_data)

            except asyncio.TimeoutError:
                await detect_captcha(page)
                await mouse_jitter(page)
                try:
                    state = await page.evaluate(POLL_STATE_JS)
                    cur_len = state.get("textLen", 0)
                    js_finished = state.get("finished", False)
                    source = state.get("source", "")

                    if not hook_source_logged and source:
                        log(f"🔍 数据通道: {source}")
                        hook_source_logged = True

                    if js_finished and not finished_by_signal:
                        log(f"🔍 Python轮询发现 finished=true (source={source}, textLen={cur_len})")
                        finished_by_signal = True
                        while not chunk_queue.empty():
                            try:
                                k2, d2 = chunk_queue.get_nowait()
                                if k2 == "body" and d2:
                                    body_data = d2
                                    if STRIP_THINK_REF_TAGS:
                                        body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                                    live_parts.append(body_data)
                                    if think_prefix_printed:
                                        print("\033[0m")
                                        think_prefix_printed = False
                                    if not stream_prefix_printed:
                                        print("AI: ", end="", flush=True)
                                        stream_prefix_printed = True
                                    print(body_data, end="", flush=True)
                                elif k2 == "think" and d2:
                                    think_parts.append(d2)
                            except asyncio.QueueEmpty:
                                break
                        break

                    if cur_len > 0 and cur_len == last_text_len:
                        if time.time() - last_active > idle_timeout:
                            log(f"⚠️ {idle_timeout}s 无新数据 (textLen={cur_len})，兜底结束")
                            break
                    else:
                        last_text_len = cur_len
                        last_active = time.time()

                except Exception as poll_err:
                    if time.time() - last_active > idle_timeout:
                        log(f"⚠️ 轮询异常且超时: {poll_err}")
                        break

        if think_prefix_printed:
            print("\033[0m")
        if stream_prefix_printed:
            print()

        final_body = "".join(live_parts)
        final_think = "".join(think_parts)

        # 剥离正文中的 [(multimodal_chat_think_N)] 引用标签
        if STRIP_THINK_REF_TAGS:
            final_body = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', final_body)

        end_reason = "FINISHED信号" if finished_by_signal else "超时兜底"
        log(f"📊 正文chunk={chunk_count}/{len(final_body)}字符, 思考chunk={think_count}/{len(final_think)}字符, 结束方式={end_reason}")

        # 输出 mime_type 统计汇总(从 debug_lines 中找 mime_summary)
        mime_summary = None
        for d in debug_lines:
            if d.startswith("mime_summary|"):
                mime_summary = d[len("mime_summary|"):]
                break
        if mime_summary:
            log(f"📋 mime_type 统计: {mime_summary}")
        else:
            log("⚠️ 未能获取 mime_type 统计(可能思考内容使用未识别的 mime_type)")

        if DUMP_RAW or DUMP_RAW_SSE:
            RAW_DUMP_DIR.mkdir(exist_ok=True)
            # 保存解析后内容
            if DUMP_RAW:
                f = RAW_DUMP_DIR / f"round{round_num}_stream.txt"
                content = ""
                if final_think:
                    content += f"=== 思考过程 ===\n{final_think}\n\n"
                content += f"=== 正文 ===\n{final_body}"
                f.write_text(content, encoding="utf-8")
                log(f"📄 已保存: {f.resolve()}")
            # 保存原始 SSE 行(诊断用)
            if DUMP_RAW_SSE and raw_sse_parts:
                f_raw = RAW_DUMP_DIR / f"round{round_num}_raw_sse.txt"
                f_raw.write_text("".join(raw_sse_parts), encoding="utf-8")
                log(f"📄 已保存原始SSE: {f_raw.resolve()} ({len(raw_sse_parts)}行)")
            # 保存 debug 信息
            if debug_lines:
                f_dbg = RAW_DUMP_DIR / f"round{round_num}_debug.txt"
                f_dbg.write_text("\n".join(debug_lines), encoding="utf-8")
                log(f"📄 已保存debug: {f_dbg.resolve()} ({len(debug_lines)}条)")

        bar = "█" * 60
        log(bar)
        log(f"✅ 第 {round_num} 轮完成（正文 {len(final_body)} 字符, 思考 {len(final_think)} 字符）")
        log(bar)
        return chunk_count, len(final_body), final_body

    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


# ============ 异步生成器版本: 供 API 使用 ============

async def stream_chat_gen(page, send_coro, round_num, idle_timeout=15) -> AsyncGenerator[tuple, None]:
    """stream_chat 的异步生成器版本, yield (kind, data) 元组
    kind: 'body' | 'think' | 'done'
    data: 字符串 (body/think) 或 dict (done: {body, think, chunk_count})"""
    cdp = await page.context.new_cdp_session(page)
    chunk_queue = asyncio.Queue()
    live_parts = []
    think_parts = []
    raw_sse_parts = []
    debug_lines = []
    chunk_count = 0
    think_count = 0
    finished_by_signal = False

    await cdp.send("Runtime.enable")
    await cdp.send("Runtime.addBinding", {"name": "__dsChunk"})
    await cdp.send("Runtime.addBinding", {"name": "__dsThink"})
    await cdp.send("Runtime.addBinding", {"name": "__dsDone"})
    await cdp.send("Runtime.addBinding", {"name": "__dsRawSse"})
    await cdp.send("Runtime.addBinding", {"name": "__dsDebug"})

    def on_binding(params):
        name = params.get("name", "")
        payload = params.get("payload", "")
        if name == "__dsChunk":
            chunk_queue.put_nowait(("body", payload))
        elif name == "__dsThink":
            chunk_queue.put_nowait(("think", payload))
        elif name == "__dsDone":
            chunk_queue.put_nowait(("done", None))
        elif name == "__dsRawSse":
            if DUMP_RAW_SSE and payload:
                raw_sse_parts.append(payload)
        elif name == "__dsDebug":
            if payload:
                debug_lines.append(payload)

    cdp.on("Runtime.bindingCalled", on_binding)

    hook_result = await page.evaluate(HOOK_JS_QWEN)
    await page.evaluate("""
        window.__ds_finished = false;
        window.__ds_lastTextLen = 0;
        window.__ds_hookSource = '';
    """)

    try:
        await send_coro
        last_active = time.time()
        last_text_len = 0
        hook_source_logged = False

        while True:
            try:
                kind, data = await asyncio.wait_for(chunk_queue.get(), timeout=0.2)
                last_active = time.time()

                if kind == "done":
                    finished_by_signal = True
                    while not chunk_queue.empty():
                        try:
                            k2, d2 = chunk_queue.get_nowait()
                            if k2 == "body" and d2:
                                body_data = d2
                                if STRIP_THINK_REF_TAGS:
                                    body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                                live_parts.append(body_data)
                                yield ("body", body_data)
                            elif k2 == "think" and d2:
                                think_parts.append(d2)
                                yield ("think", d2)
                        except asyncio.QueueEmpty:
                            break
                    break

                elif kind == "think":
                    think_count += 1
                    think_parts.append(data)
                    yield ("think", data)

                elif kind == "body":
                    chunk_count += 1
                    if data:
                        body_data = data
                        if STRIP_THINK_REF_TAGS:
                            body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                        live_parts.append(body_data)
                        yield ("body", body_data)

            except asyncio.TimeoutError:
                await detect_captcha(page)
                await mouse_jitter(page)
                try:
                    state = await page.evaluate(POLL_STATE_JS)
                    cur_len = state.get("textLen", 0)
                    js_finished = state.get("finished", False)
                    source = state.get("source", "")

                    if not hook_source_logged and source:
                        log(f"🔍 数据通道: {source}")
                        hook_source_logged = True

                    if js_finished and not finished_by_signal:
                        log(f"🔍 Python轮询发现 finished=true (source={source}, textLen={cur_len})")
                        finished_by_signal = True
                        while not chunk_queue.empty():
                            try:
                                k2, d2 = chunk_queue.get_nowait()
                                if k2 == "body" and d2:
                                    body_data = d2
                                    if STRIP_THINK_REF_TAGS:
                                        body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                                    live_parts.append(body_data)
                                    yield ("body", body_data)
                                elif k2 == "think" and d2:
                                    think_parts.append(d2)
                                    yield ("think", d2)
                            except asyncio.QueueEmpty:
                                break
                        break

                    if cur_len > 0 and cur_len == last_text_len:
                        if time.time() - last_active > idle_timeout:
                            log(f"⚠️ {idle_timeout}s 无新数据 (textLen={cur_len})，兜底结束")
                            break
                    else:
                        last_text_len = cur_len
                        last_active = time.time()

                except Exception as poll_err:
                    if time.time() - last_active > idle_timeout:
                        log(f"⚠️ 轮询异常且超时: {poll_err}")
                        break

        final_body = "".join(live_parts)
        final_think = "".join(think_parts)

        if STRIP_THINK_REF_TAGS:
            final_body = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', final_body)

        end_reason = "FINISHED信号" if finished_by_signal else "超时兜底"
        log(f"📊 正文chunk={chunk_count}/{len(final_body)}字符, 思考chunk={think_count}/{len(final_think)}字符, 结束方式={end_reason}")

        mime_summary = None
        for d in debug_lines:
            if d.startswith("mime_summary|"):
                mime_summary = d[len("mime_summary|"):]
                break
        if mime_summary:
            log(f"📋 mime_type 统计: {mime_summary}")

        if DUMP_RAW or DUMP_RAW_SSE:
            RAW_DUMP_DIR.mkdir(exist_ok=True)
            if DUMP_RAW:
                f = RAW_DUMP_DIR / f"round{round_num}_stream.txt"
                content = ""
                if final_think:
                    content += f"=== 思考过程 ===\n{final_think}\n\n"
                content += f"=== 正文 ===\n{final_body}"
                f.write_text(content, encoding="utf-8")
            if DUMP_RAW_SSE and raw_sse_parts:
                f_raw = RAW_DUMP_DIR / f"round{round_num}_raw_sse.txt"
                f_raw.write_text("".join(raw_sse_parts), encoding="utf-8")
            if debug_lines:
                f_dbg = RAW_DUMP_DIR / f"round{round_num}_debug.txt"
                f_dbg.write_text("\n".join(debug_lines), encoding="utf-8")

        log(f"✅ 第 {round_num} 轮完成（正文 {len(final_body)} 字符, 思考 {len(final_think)} 字符）")

        yield ("done", {
            "body": final_body,
            "think": final_think,
            "chunk_count": chunk_count,
            "think_count": think_count,
            "mime_summary": mime_summary,
        })

    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


# ============ 会话管理 ============

async def reset_session(new_page=True):
    """重置会话: 关闭旧标签页, 新建标签页, 返回新的 page"""
    page = _app_state["page"]
    context = _app_state["context"]
    browser = _app_state["browser"]

    if not page or not context or not browser:
        log("❌ 浏览器未初始化, 无法重置会话")
        return page

    log("🔄 重置会话...")

    # 1. 关闭旧标签页
    try:
        if new_page:
            await page.close()
            log("   已关闭旧标签页")
    except Exception as e:
        log(f"   ⚠️ 关闭旧标签页异常: {e}")

    # 2. 新建标签页
    try:
        new_page = await context.new_page()
        log("   已新建标签页")
    except Exception as e:
        log(f"   ❌ 新建标签页失败: {e}")
        return page

    # 3. 导航到千问
    try:
        await new_page.goto(QWEN_URL, wait_until="domcontentloaded")
        await new_page.wait_for_timeout(3000)
        log("   已导航到千问首页")
    except Exception as e:
        log(f"   ⚠️ 导航异常: {e}")

    # 4. 注入 stealth 补丁
    try:
        await new_page.add_init_script(STEALTH_PATCH_JS)
    except Exception:
        pass
    try:
        await new_page.evaluate(STEALTH_PATCH_JS)
    except Exception:
        pass

    # 5. 等待登录
    if not await wait_for_login_then_chat(new_page, LOGIN_TIMEOUT_SEC):
        log("❌ 新标签页登录超时")
        return page

    # 6. 确保模式
    await ensure_modes(new_page, ENABLE_MODES)
    if DEFAULT_MODEL:
        await switch_model(new_page, DEFAULT_MODEL)

    # 7. 更新状态
    _app_state["page"] = new_page
    log("✅ 会话已重置")
    return new_page


def _prompt_midscene_enabled():
    """交互式询问是否启用 Midscene OS 级操作
    
    如果已通过命令行或环境变量确定,则跳过询问
    """
    global MIDSCENE_ENABLED, MIDSCENE_BASE_URL, _MIDSCENE_EXTERNAL_SET
    
    # 如果已通过命令行或环境变量确定,跳过交互式询问
    if _MIDSCENE_EXTERNAL_SET:
        status = "启用" if MIDSCENE_ENABLED else "未启用"
        log(f"   Midscene: {status} (已通过外部配置确定)")
        if MIDSCENE_ENABLED:
            _verify_midscene_service()
        return
    
    print()
    print("=" * 50)
    print("  Midscene OS 级自动化(路线 A)")
    print("  基于视觉识别 + OS 级键鼠操作的反检测方案")
    print("=" * 50)
    print()
    print("  启用后,滑块验证将走 Midscene 路径:")
    print("    1. AI 视觉定位滑块(零 DOM 痕迹)")
    print("    2. OS 级键鼠拖拽(最高反检测强度)")
    print()
    print("  前提条件:")
    print("    • Midscene Node.js 服务已启动(端口 3456)")
    print("    • macOS 辅助功能权限已授权")
    print("=" * 50)
    
    while True:
        try:
            choice = input("\n  是否启用 Midscene? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            MIDSCENE_ENABLED = False
            log("   未启用 Midscene")
            return
        
        if choice in ('y', 'yes'):
            MIDSCENE_ENABLED = True
            log("   ✅ 已启用 Midscene OS 级操作")
            
            # 可选:自定义 Midscene 服务地址
            try:
                url = input(f"   Midscene 服务地址 [默认 {MIDSCENE_BASE_URL}]: ").strip()
                if url:
                    MIDSCENE_BASE_URL = url
            except (EOFError, KeyboardInterrupt):
                pass
            
            _verify_midscene_service()
            return
        elif choice in ('', 'n', 'no'):
            MIDSCENE_ENABLED = False
            log("   未启用 Midscene,滑块将使用 Playwright 处理")
            return
        else:
            print("   请输入 y 或 n")


def _verify_midscene_service():
    """验证 Midscene 服务连通性"""
    global MIDSCENE_BASE_URL
    
    try:
        import urllib.request
        with urllib.request.urlopen(f"{MIDSCENE_BASE_URL}/health", timeout=2) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "ok" and data.get("agentInitialized"):
                log(f"   ✅ Midscene 服务就绪 (agent 已初始化)")
            elif data.get("status") == "ok":
                log(f"   ⚠️  Midscene 服务在线,但 agent 未初始化")
                log(f"      首次调用时会自动初始化(可能需要几秒)")
            else:
                log(f"   ⚠️  Midscene 服务状态异常: {data}")
    except Exception:
        log(f"   ⚠️  无法连接到 Midscene 服务 ({MIDSCENE_BASE_URL})")
        log(f"      请先启动: cd midscene-computer && ./start.sh")
        log(f"      当前将继续运行,但滑块会回退到 Playwright 处理")


async def main():
    log("脚本启动")
    
    # 交互式询问 Midscene(仅在非 API 模式下,且未通过命令行/环境变量确定时)
    global MIDSCENE_ENABLED
    # 只有初始值 False 且未显式设置时才询问
    # 但命令行和环境变量的处理在 __main__ 中已完成
    # 这里再检查一次:如果 MIDSCENE_ENABLED 仍是初始值 False,且没有显式禁用的意图
    # 简化处理:非 API 模式下总是询问一次(用户可能想临时切换)
    _prompt_midscene_enabled()
    
    chrome_proc = launch_chrome()
    attached = (chrome_proc is None)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        log("CDP 连接成功")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page, is_reused = await find_or_create_qwen_page(context)

        log("🔒 注入 stealth 环境补丁...")
        try:
            await page.add_init_script(STEALTH_PATCH_JS)
        except Exception:
            pass
        try:
            await page.evaluate(STEALTH_PATCH_JS)
            log("   ✅ 补丁已注入")
        except Exception as e:
            log(f"   ⚠️  注入部分受限: {e}")

        if not is_reused:
            await page.goto(QWEN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        if not await wait_for_login_then_chat(page, LOGIN_TIMEOUT_SEC):
            log("❌ 登录超时")
            await browser.close()
            return

        input_el = await find_element(page, INPUT_SELECTORS, "输入框")
        if not input_el:
            log("❌ 找不到输入框")
            await browser.close()
            return

        await ensure_modes(page, ENABLE_MODES)

        if DEFAULT_MODEL:
            await switch_model(page, DEFAULT_MODEL)

        if DEFAULT_CHAT_MODE:
            await switch_chat_mode(page, DEFAULT_CHAT_MODE)

        send_el = await find_element(page, SEND_SELECTORS, "发送按钮")

        mode = "附加" if attached else "新启动"
        tab = "复用" if is_reused else "新建"
        log(f"✅ 就绪（{mode}/{tab}）\n")

        round_num = 0
        while True:
            raw = await prompt_query("You: ", COMMANDS)
            if raw is None:
                break
            # 归一化: /models -> models, /model x -> model x; 未命中的 /xxx 原样作为聊天
            query = normalize_command(raw, COMMANDS)
            if not query or query.lower() == "quit":
                break

            if query.lower() == "help":
                print_help(COMMANDS, "千问交互命令")
                continue

            if query.lower() == "models":
                await list_models(page)
                continue

            if query.lower().startswith("model "):
                model_name = query[6:].strip()
                if model_name:
                    await switch_model(page, model_name)
                else:
                    log("用法: /model <模型名>")
                continue

            if query.lower() == "chatmodes":
                await list_chat_modes(page)
                continue

            if query.lower().startswith("chatmode "):
                mode_name = query[9:].strip()
                if mode_name:
                    await switch_chat_mode(page, mode_name)
                else:
                    log("用法: /chatmode <模式名>")
                continue

            round_num += 1
            log(f"===== 第 {round_num} 轮 =====")

            async def do_send(q=query):
                import random
                ie = await find_element(page, INPUT_SELECTORS, "输入框")
                se = await find_element(page, SEND_SELECTORS, "发送按钮")
                if not ie:
                    log("❌ 输入框丢失")
                    return
                try:
                    box = await ie.bounding_box()
                    if box:
                        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                        await page.mouse.move(tx, ty, steps=random.randint(8, 15))
                        await page.wait_for_timeout(random.randint(80, 200))
                        await page.mouse.click(tx, ty)
                        await page.wait_for_timeout(random.randint(100, 250))
                        await ie.click()
                        await page.wait_for_timeout(random.randint(100, 200))
                except Exception:
                    try:
                        await ie.click()
                    except Exception:
                        pass
                try:
                    await ie.fill("")
                except Exception:
                    pass
                chunks = []
                cur = []
                for ch in q:
                    cur.append(ch)
                    if len(cur) >= random.randint(2, 3):
                        chunks.append("".join(cur))
                        cur = []
                if cur:
                    chunks.append("".join(cur))
                prev_ch = ""
                for i, chunk in enumerate(chunks):
                    for ch in chunk:
                        delay = _human_keystroke_delay_ms(prev_ch, ch)
                        await ie.type(ch, delay=0)
                        await page.wait_for_timeout(delay)
                        prev_ch = ch
                    if i < len(chunks) - 1:
                        await page.wait_for_timeout(random.randint(120, 350))
                await page.wait_for_timeout(random.randint(200, 470))
                if se:
                    try:
                        sbox = await se.bounding_box()
                        if sbox:
                            sx = sbox["x"] + sbox["width"] / 2
                            sy = sbox["y"] + sbox["height"] / 2
                            await page.mouse.move(sx, sy, steps=random.randint(5, 10))
                            await page.wait_for_timeout(random.randint(50, 150))
                    except Exception:
                        pass
                    await se.click()
                else:
                    await ie.press("Enter")
                log("已发送")

            try:
                await stream_chat(page, do_send(), round_num)
            except Exception as e:
                log(f"❌ {type(e).__name__}: {e}")

            log(f"===== 第 {round_num} 轮结束 =====\n")

        await browser.close()
        log("退出")


# ============ API 服务器 ============

def _build_chat_chunk(content=None, reasoning=None, model=None, chunk_id=None, finish=None, usage=None):
    """构建 OpenAI 兼容的 chat chunk"""
    now = int(time.time())
    msg = {
        "id": chunk_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": now,
        "model": model or API_MODEL,
        "choices": [{
            "index": 0,
            "delta": {},
        }],
    }
    if reasoning:
        msg["choices"][0]["delta"]["reasoning_content"] = reasoning
    if content:
        msg["choices"][0]["delta"]["content"] = content
    if finish:
        msg["choices"][0]["finish_reason"] = finish
    if usage:
        msg["usage"] = usage
    return msg


def _build_chat_response(content=None, reasoning=None, model=None, chunk_id=None, usage=None):
    """构建 OpenAI 兼容的完整响应"""
    now = int(time.time())
    return {
        "id": chunk_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": now,
        "model": model or API_MODEL,
        "usage": usage or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        },
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content or "",
                "reasoning_content": reasoning or "",
            },
            "finish_reason": "stop",
        }],
    }


def estimate_tokens(text):
    """粗略估算 token 数 (中文约 1.5 token/字, 英文约 0.25 token/字符)"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 1.5 + other_chars * 0.25)


async def _send_message_api(page, query):
    """API 模式下的消息发送, 简化版"""
    import random
    ie = await find_element(page, INPUT_SELECTORS, "输入框")
    se = await find_element(page, SEND_SELECTORS, "发送按钮")
    if not ie:
        raise RuntimeError("输入框丢失")

    # 点击输入框
    try:
        box = await ie.bounding_box()
        if box:
            tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.move(tx, ty, steps=random.randint(8, 15))
            await page.wait_for_timeout(random.randint(80, 200))
            await page.mouse.click(tx, ty)
            await page.wait_for_timeout(random.randint(100, 250))
    except Exception:
        pass

    try:
        await ie.click()
    except Exception:
        pass

    try:
        await ie.fill("")
    except Exception:
        pass

    # 分段输入
    chunks = []
    cur = []
    for ch in query:
        cur.append(ch)
        if len(cur) >= random.randint(2, 3):
            chunks.append("".join(cur))
            cur = []
    if cur:
        chunks.append("".join(cur))

    prev_ch = ""
    for i, chunk in enumerate(chunks):
        for ch in chunk:
            delay = _human_keystroke_delay_ms(prev_ch, ch)
            await ie.type(ch, delay=0)
            await page.wait_for_timeout(delay)
            prev_ch = ch
        if i < len(chunks) - 1:
            await page.wait_for_timeout(random.randint(120, 350))

    await page.wait_for_timeout(random.randint(200, 470))

    if se:
        try:
            sbox = await se.bounding_box()
            if sbox:
                sx = sbox["x"] + sbox["width"] / 2
                sy = sbox["y"] + sbox["height"] / 2
                await page.mouse.move(sx, sy, steps=random.randint(5, 10))
                await page.wait_for_timeout(random.randint(50, 150))
        except Exception:
            pass
        await se.click()
    else:
        await ie.press("Enter")


async def create_api_app():
    """创建 aiohttp 应用"""
    app = web.Application()
    lock = asyncio.Lock()
    _app_state["lock"] = lock

    async def handle_chat(request):
        # API Key 鉴权
        if API_KEY:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != API_KEY:
                return web.json_response({"error": {"message": "Unauthorized"}}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"message": "Invalid JSON"}}, status=400)

        messages = body.get("messages", [])
        if not messages:
            return web.json_response({"error": {"message": "messages required"}}, status=400)

        stream = body.get("stream", True)
        new_session = body.get("new_session", False)
        user_msg = messages[-1].get("content", "")
        if isinstance(user_msg, list):
            user_msg = " ".join(str(c) for c in user_msg if isinstance(c, str))

        async with lock:
            page = _app_state["page"]
            if not page:
                return web.json_response({"error": {"message": "Page not ready"}}, status=503)

            # 会话重置
            if new_session:
                log("🔄 API 请求触发新会话")
                page = await reset_session(new_page=True)
                if not page:
                    return web.json_response({"error": {"message": "Session reset failed"}}, status=500)

            _app_state["round_num"] += 1
            round_num = _app_state["round_num"]
            chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            log(f"===== 第 {round_num} 轮 (API) =====")

            async def do_send():
                await _send_message_api(page, user_msg)

            if stream:
                async def stream_generator():
                    final_body = ""
                    final_think = ""
                    try:
                        async for kind, data in stream_chat_gen(page, do_send(), round_num):
                            if kind == "body":
                                final_body += data
                                yield f"data: {json.dumps(_build_chat_chunk(content=data, model=API_MODEL, chunk_id=chat_id), ensure_ascii=False)}\n\n"
                            elif kind == "think":
                                final_think += data
                                yield f"data: {json.dumps(_build_chat_chunk(reasoning=data, model=API_MODEL, chunk_id=chat_id), ensure_ascii=False)}\n\n"
                            elif kind == "done":
                                final_body = data.get("body", final_body)
                                final_think = data.get("think", final_think)
                    except Exception as e:
                        log(f"❌ API 流式异常: {type(e).__name__}: {e}")
                        yield f"data: {json.dumps(_build_chat_chunk(content=f'[error] {e}', model=API_MODEL, chunk_id=chat_id), ensure_ascii=False)}\n\n"

                    prompt_tok = estimate_tokens(user_msg)
                    body_tok = estimate_tokens(final_body)
                    think_tok = estimate_tokens(final_think)
                    usage = {
                        "prompt_tokens": prompt_tok,
                        "completion_tokens": body_tok,
                        "reasoning_tokens": think_tok,
                        "total_tokens": prompt_tok + body_tok + think_tok,
                    }
                    yield f"data: {json.dumps(_build_chat_chunk(model=API_MODEL, chunk_id=chat_id, finish='stop', usage=usage), ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                # 正确写法
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    }
                )
                await resp.prepare(request)
                try:
                    async for chunk in stream_generator():
                        await resp.write(chunk.encode("utf-8"))
                except Exception:
                    pass
                return resp

            # 非流式
            final_body = ""
            final_think = ""
            try:
                async for kind, data in stream_chat_gen(page, do_send(), round_num):
                    if kind == "body":
                        final_body += data
                    elif kind == "think":
                        final_think += data
            except Exception as e:
                log(f"❌ API 非流式异常: {type(e).__name__}: {e}")
                return web.json_response({"error": {"message": str(e)}}, status=500)

            prompt_tok = estimate_tokens(user_msg)
            body_tok = estimate_tokens(final_body)
            think_tok = estimate_tokens(final_think)
            usage = {
                "prompt_tokens": prompt_tok,
                "completion_tokens": body_tok,
                "reasoning_tokens": think_tok,
                "total_tokens": prompt_tok + body_tok + think_tok,
            }
            return web.json_response(_build_chat_response(final_body, final_think, API_MODEL, chat_id, usage))

    async def handle_models(request):
        return web.json_response({
            "object": "list",
            "data": [{
                "id": API_MODEL,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "qianwen",
            }]
        })

    async def handle_reset(request):
        # API Key 鉴权
        if API_KEY:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != API_KEY:
                return web.json_response({"error": {"message": "Unauthorized"}}, status=401)

        async with lock:
            page = _app_state["page"]
            if not page:
                return web.json_response({"error": {"message": "Page not ready"}}, status=503)
            new_page = await reset_session(new_page=True)
            if new_page:
                return web.json_response({"status": "ok", "message": "Session reset", "page": "new"})
            return web.json_response({"error": {"message": "Reset failed"}}, status=500)

    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/session/reset", handle_reset)
    return app


async def run_api_server(host: str, port: int):
    """启动 API 服务器"""
    global API_KEY
    log("脚本启动（API 模式）")
    chrome_proc = launch_chrome()
    attached = (chrome_proc is None)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        log("CDP 连接成功")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page, is_reused = await find_or_create_qwen_page(context)

        log("🔒 注入 stealth 环境补丁...")
        try:
            await page.add_init_script(STEALTH_PATCH_JS)
        except Exception:
            pass
        try:
            await page.evaluate(STEALTH_PATCH_JS)
            log("   ✅ 补丁已注入")
        except Exception as e:
            log(f"   ⚠️ 注入部分受限: {e}")

        if not is_reused:
            await page.goto(QWEN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        if not await wait_for_login_then_chat(page, LOGIN_TIMEOUT_SEC):
            log("❌ 登录超时")
            await browser.close()
            return

        await ensure_modes(page, ENABLE_MODES)
        if DEFAULT_MODEL:
            await switch_model(page, DEFAULT_MODEL)

        send_el = await find_element(page, SEND_SELECTORS, "发送按钮")

        _app_state["page"] = page
        _app_state["browser"] = browser
        _app_state["context"] = context
        _app_state["lock"] = asyncio.Lock()

        mode = "附加" if attached else "新启动"
        tab = "复用" if is_reused else "新建"
        log(f"✅ 就绪（{mode}/{tab}）")
        log(f"🚀 API 服务启动: http://{host}:{port}")
        log(f"   POST /v1/chat/completions  - 聊天")
        log(f"   POST /v1/session/reset    - 重置会话")
        log(f"   GET  /v1/models           - 模型列表")
        if API_KEY:
            log(f"   API Key: {API_KEY}")
        log(f"   参数 new_session=true 可在请求时重置会话")
        log(f"   (Ctrl+C 退出)\n")

        app = await create_api_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        try:
            # 保持运行
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()
            await browser.close()
            log("退出")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Qianwen Web Hook")
    parser.add_argument("--api", action="store_true", help="启动 OpenAI 兼容 API 服务")
    parser.add_argument("--host", default=API_HOST, help=f"API 监听地址（默认 {API_HOST}）")
    parser.add_argument("--port", type=int, default=API_PORT, help=f"API 监听端口（默认 {API_PORT}）")
    parser.add_argument("--api-key", default=API_KEY, help="API 鉴权密钥（留空则不鉴权）")
    parser.add_argument("--model", default=API_MODEL, help=f"API 模型名（默认 {API_MODEL}）")
    parser.add_argument("--midscene", dest="midscene", default=None,
                        help="启用 Midscene OS 级操作 (true/false, 优先级最高)")
    parser.add_argument("--midscene-url", dest="midscene_url", default=None,
                        help=f"Midscene 服务地址 (默认 {MIDSCENE_BASE_URL})")
    args = parser.parse_args()

    if args.api_key:
        API_KEY = args.api_key
    if args.model:
        API_MODEL = args.model

    # ============ 确定 MIDSCENE_ENABLED(三种方式,优先级从高到低) ============
    # 1. 命令行参数 --midscene true/false
    if args.midscene is not None:
        if args.midscene.lower() in ('true', '1', 'yes', 'y'):
            MIDSCENE_ENABLED = True
        elif args.midscene.lower() in ('false', '0', 'no', 'n'):
            MIDSCENE_ENABLED = False
        else:
            print(f"❌ --midscene 参数无效: {args.midscene} (应为 true 或 false)")
            sys.exit(1)
        _MIDSCENE_EXTERNAL_SET = True
        print(f"📌 Midscene (命令行): {'启用' if MIDSCENE_ENABLED else '未启用'}")
    # 2. 环境变量 MIDSCENE_ENABLED
    elif os.environ.get('MIDSCENE_ENABLED', '').lower() in ('true', '1', 'yes'):
        MIDSCENE_ENABLED = True
        _MIDSCENE_EXTERNAL_SET = True
        print(f"📌 Midscene (环境变量): 启用")
    elif os.environ.get('MIDSCENE_ENABLED', '').lower() in ('false', '0', 'no'):
        MIDSCENE_ENABLED = False
        _MIDSCENE_EXTERNAL_SET = True
        print(f"📌 Midscene (环境变量): 未启用")
    # 3. 交互式询问(仅非 API 模式,在 main() 中调用)

    # Midscene 服务地址(命令行优先)
    if args.midscene_url:
        MIDSCENE_BASE_URL = args.midscene_url
        print(f"📌 Midscene 服务地址: {MIDSCENE_BASE_URL}")
    elif os.environ.get('MIDSCENE_BASE_URL'):
        MIDSCENE_BASE_URL = os.environ['MIDSCENE_BASE_URL']

    # API 模式下,如果启用了 Midscene,提前验证服务
    if args.api and MIDSCENE_ENABLED:
        print()
        log("🔌 验证 Midscene 服务连通性...")
        try:
            import urllib.request
            with urllib.request.urlopen(f"{MIDSCENE_BASE_URL}/health", timeout=3) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "ok":
                    log(f"   ✅ Midscene 服务就绪")
                else:
                    log(f"   ⚠️  Midscene 服务状态异常")
        except Exception:
            log(f"   ⚠️  无法连接 Midscene 服务,滑块将回退到 Playwright")
            log(f"      启动命令: cd midscene-computer && ./start.sh")

    try:
        if args.api:
            asyncio.run(run_api_server(args.host, args.port))
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
