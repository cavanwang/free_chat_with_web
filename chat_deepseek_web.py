import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
import socket
import uuid
from pathlib import Path
from typing import AsyncGenerator, Optional
from playwright.async_api import async_playwright

import session_store              # 与 qwen 共享的会话历史存储(网关持有会话正本)
import gateway_common as gwc      # 与 qwen 共用的上下文拼装/等待判定纯逻辑

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
    "/chatmodes": "查看对话模式列表",
    "/mode":      "切换对话模式, 用法: /mode <模式名>",
    "/quit":      "退出程序",
}


# ============ 配置区 ============
CHROME_PATH = r"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEBUG_PORT = 9222
USER_DATA_DIR = Path("./deepseek_chrome_profile")
DEEPSEEK_URL = "https://chat.deepseek.com/"
DEEPSEEK_HOST = "chat.deepseek.com"
HEADLESS = False  # 默认有头模式, 可实时看到画面并手动交互
LOGIN_TIMEOUT_SEC = 600
REUSE_EXISTING_TAB = True
DUMP_RAW = True
RAW_DUMP_DIR = Path("./raw_dumps")
STRIP_CITATIONS = True
STREAM_OUTPUT = True
PRINT_STREAM_PREFIX = True
ENABLE_MODES = ["深度思考", "智能搜索"]
DEFAULT_CHAT_MODE = ""  # "专家模式" 或 "识图模式"，留空保持默认
# ===============================


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S") + f",{int(time.time() * 1000) % 1000:03d}"
    print(f"[执行日志] {ts} {msg}", flush=True)


INPUT_SELECTORS = [
    'textarea[placeholder="给 DeepSeek 发送消息"]',
    'textarea[placeholder*="发送消息"]',
    'textarea[placeholder*="DeepSeek"]',
    'div[contenteditable="true"]',
    'textarea',
    '[role="textbox"]',
]

SEND_SELECTORS = [
    'button[aria-label*="发送"]',
    'button[aria-label*="Send"]',
    'button:has(svg)',
    'button[type="submit"]',
]

LOGIN_DETECTORS = [
    'input[placeholder*="手机"]', 'input[placeholder*="Phone"]', 'input[type="tel"]',
    'input[placeholder*="邮箱"]', 'input[placeholder*="Email"]', 'input[type="email"]',
    'text=/扫码/', 'text=/Scan/', 'text=/微信登录/', 'text=/WeChat/',
    'text=/验证码/', 'text=/获取验证码/',
    'button:has-text("登录")', 'button:has-text("Log in")', 'button:has-text("Sign in")',
]


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
        if is_chrome_usable(DEBUG_PORT):
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


async def find_or_create_deepseek_page(context):
    if REUSE_EXISTING_TAB:
        for pg in context.pages:
            try:
                if DEEPSEEK_HOST in pg.url:
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
            if not chip:
                log(f"   ⚠️ 未找到「{name}」按钮")
                continue
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
        except Exception as e:
            log(f"   ❌ 「{name}」出错: {e}")
    log("🎛️  模式设置完成")


# ============================================================
# 对话模式切换（快速模式 / 专家模式 / 识图模式）
# ============================================================
CHAT_MODE_MAP = {
    "快速模式": "default",
    "专家模式": "expert",
    "识图模式": "vision",
}


async def list_chat_modes(page):
    log("📋 获取对话模式列表...")
    try:
        group = page.locator('[role="radiogroup"][data-item-count="3"]')
        if await group.count() == 0:
            group = page.locator('[role="radiogroup"]')
        count = await group.count()
        if count == 0:
            log("   模式切换器不可见（可能已进入对话），重置到首页...")
            if not await _reset_to_home(page):
                return
            group = page.locator('[role="radiogroup"][data-item-count="3"]')
            if await group.count() == 0:
                group = page.locator('[role="radiogroup"]')
            count = await group.count()
            if count == 0:
                log("   ⚠️ 重置后仍未找到对话模式容器")
                return
        items = group.first.locator('[data-model-type]')
        n = await items.count()
        log(f"   发现 {n} 个对话模式:")
        for i in range(n):
            try:
                el = items.nth(i)
                mt = await el.get_attribute("data-model-type") or ""
                aria = await el.get_attribute("aria-checked") or ""
                name = {"default": "快速模式", "expert": "专家模式", "vision": "识图模式"}.get(mt, mt)
                log(f"   - {name} (data-model-type={mt}, aria-checked={aria})")
            except Exception:
                pass
    except Exception as e:
        log(f"   ❌ 获取失败: {e}")


async def _inject_hook(page):
    """重新注入 hook JS 到页面（页面导航后调用）"""
    try:
        await page.evaluate(HOOK_JS_V8)
        await page.evaluate(f"""
            window.__ds_strip_citations = {str(STRIP_CITATIONS).lower()};
            window.__ds_finished = false;
            window.__ds_lastTextLen = 0;
            window.__ds_hookSource = '';
        """)
        log("   🔌 Hook JS 已重新注入")
        return True
    except Exception as e:
        log(f"   ⚠️ Hook 注入失败: {e}")
        return False


async def _reset_to_home(page):
    """导航回首页，确保模式切换器可见"""
    log("   🔄 导航到首页以确保模式切换器可见...")
    try:
        await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1000)
        await _inject_hook(page)
        await page.wait_for_timeout(800)
        log("   ✅ 已重置到首页")
        return True
    except Exception as e:
        log(f"   ❌ 导航失败: {e}")
        return False


async def start_new_chat(page):
    """开启一个干净的新会话。DeepSeek 里导航回首页(_reset_to_home)即为全新会话。
    返回可用的 page(与入参相同, 同标签页内)。
    """
    ok = await _reset_to_home(page)
    if not ok:
        log("⚠️ 新建会话(重置首页)失败, 继续尝试当前页面")
    return page


async def switch_chat_mode(page, mode_name):
    log(f"🔄 切换对话模式 → 「{mode_name}」")
    target_type = CHAT_MODE_MAP.get(mode_name)
    if not target_type:
        log(f"   ❌ 未知模式「{mode_name}」，支持: {list(CHAT_MODE_MAP.keys())}")
        return False

    try:
        el = page.locator(f'[data-model-type="{target_type}"]')
        count = await el.count()

        if count == 0:
            log("   模式切换器不可见（可能已进入对话），重置到首页...")
            if not await _reset_to_home(page):
                return False
            el = page.locator(f'[data-model-type="{target_type}"]')
            count = await el.count()
            if count == 0:
                log(f"   ❌ 重置后仍未找到 data-model-type={target_type}")
                return False

        aria = await el.first.get_attribute("aria-checked") or "false"
        if aria == "true":
            log(f"   ✅ 已是「{mode_name}」")
            return True

        await el.first.click()
        await page.wait_for_timeout(500)
        aria2 = await el.first.get_attribute("aria-checked") or ""
        ok = aria2 == "true"
        log(f"   {'✅' if ok else '⚠️'} 已切换到「{mode_name}」(aria-checked={aria2})")
        return ok
    except Exception as e:
        log(f"   ❌ 切换失败: {e}")
        return False


# ============================================================
# Hook JS v8 —— 同时 hook XHR 和 fetch
# ============================================================
HOOK_JS_V8 = r"""
() => {
    if (window.__ds_hook_v8) return 'already';
    window.__ds_hook_v8 = true;

    window.__ds_finished = false;
    window.__ds_lastTextLen = 0;
    window.__ds_hookSource = '';

    function stripCitations(text) {
        if (window.__ds_strip_citations) {
            return text.replace(/\[(?:citation|reference):\d+\]/g, '');
        }
        return text;
    }

    function makeProcessor() {
        let cursor_text = '';
        let phase = 'idle';
        let done = false;

        function processChunk(text) {
            if (done) return;
            cursor_text += text;
            window.__ds_lastTextLen = (window.__ds_lastTextLen || 0) + text.length;

            const nl = cursor_text.lastIndexOf('\n');
            if (nl === -1) return;
            const complete = cursor_text.substring(0, nl + 1);
            cursor_text = cursor_text.substring(nl + 1);

            let bodyOut = '';
            let thinkOut = '';
            let isFinished = false;
            const lines = complete.split('\n');

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed.startsWith('data:')) continue;
                const payload = trimmed.substring(5).trim();
                if (!payload) continue;

                let obj;
                try { obj = JSON.parse(payload); } catch(e) { continue; }

                const p = obj.p || '';
                const o = obj.o || '';
                const v = obj.v;

                if (p === 'response/fragments/-1/content' && o === 'APPEND' && phase !== 'response') {
                    phase = 'think';
                    continue;
                }

                if (p === 'response/fragments' && o === 'APPEND' && Array.isArray(v)) {
                    const hasResponse = v.some(item => item && item.type === 'RESPONSE');
                    if (hasResponse) {
                        phase = 'response';
                        for (const item of v) {
                            if (item && item.type === 'RESPONSE' && typeof item.content === 'string' && item.content) {
                                bodyOut += item.content;
                            }
                        }
                    }
                    continue;
                }

                if (p === 'response/fragments/-1' && o === 'BATCH' && Array.isArray(v)) {
                    for (const sub of v) {
                        if (sub && sub.p === 'content' && sub.o === 'APPEND' && typeof sub.v === 'string') {
                            if (phase === 'response') bodyOut += sub.v;
                            else if (phase === 'think') thinkOut += sub.v;
                        }
                    }
                    continue;
                }

                if (p === 'response/status' && v === 'FINISHED') {
                    isFinished = true;
                    continue;
                }
                if (p === 'response' && o === 'BATCH' && Array.isArray(v)) {
                    for (const item of v) {
                        if (item && item.p === 'quasi_status' && item.v === 'FINISHED') {
                            isFinished = true;
                        }
                    }
                    continue;
                }

                if (phase === 'response') {
                    if (p === 'response/fragments/-1/content' && typeof v === 'string') {
                        bodyOut += v;
                        continue;
                    }
                    if (!p && !o && typeof v === 'string') {
                        bodyOut += v;
                        continue;
                    }
                }

                if (phase === 'think') {
                    if (!p && !o && typeof v === 'string') {
                        thinkOut += v;
                        continue;
                    }
                }
            }

            if (thinkOut) {
                try { window.__dsThink(stripCitations(thinkOut)); } catch(e){}
            }
            if (bodyOut) {
                try { window.__dsChunk(stripCitations(bodyOut)); } catch(e){}
            }
            if (isFinished) {
                done = true;
                window.__ds_finished = true;
                try { window.__dsDone(); } catch(e){}
            }
        }

        function forceFinish() {
            if (done) return;
            if (cursor_text.trim()) {
                const remaining = cursor_text;
                cursor_text = '';
                let bodyOut = '';
                let thinkOut = '';
                const lines = remaining.split('\n');
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed.startsWith('data:')) continue;
                    const payload = trimmed.substring(5).trim();
                    if (!payload) continue;
                    let obj;
                    try { obj = JSON.parse(payload); } catch(e) { continue; }
                    const p = obj.p || '';
                    const o = obj.o || '';
                    const v = obj.v;
                    if (p === 'response/fragments/-1' && o === 'BATCH' && Array.isArray(v)) {
                        for (const sub of v) {
                            if (sub && sub.p === 'content' && sub.o === 'APPEND' && typeof sub.v === 'string') {
                                if (phase === 'response') bodyOut += sub.v;
                                else if (phase === 'think') thinkOut += sub.v;
                            }
                        }
                    }
                    if (phase === 'response' && p === 'response/fragments/-1/content' && typeof v === 'string') {
                        bodyOut += v;
                    }
                    if (phase === 'think' && !p && !o && typeof v === 'string') {
                        thinkOut += v;
                    }
                    if (p === 'response/status' && v === 'FINISHED') {
                        window.__ds_finished = true;
                    }
                    if (p === 'response' && o === 'BATCH' && Array.isArray(v)) {
                        for (const item of v) {
                            if (item && item.p === 'quasi_status' && item.v === 'FINISHED') {
                                window.__ds_finished = true;
                            }
                        }
                    }
                }
                if (thinkOut) try { window.__dsThink(stripCitations(thinkOut)); } catch(e){}
                if (bodyOut) try { window.__dsChunk(stripCitations(bodyOut)); } catch(e){}
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
    const OrigXHR = window.XMLHttpRequest;
    window.XMLHttpRequest = function(...args) {
        const xhr = new OrigXHR(...args);
        const origOpen = xhr.open.bind(xhr);
        const origSend = xhr.send.bind(xhr);

        xhr.open = function(method, url, ...rest) {
            this.__ds_url = (typeof url === 'string') ? url : '';
            return origOpen(method, url, ...rest);
        };

        xhr.send = function(...a) {
            if (this.__ds_url && this.__ds_url.includes('/api/v0/chat/completion')) {
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
    const origFetch = window.fetch;
    window.fetch = function(...args) {
        const url = (typeof args[0] === 'string') ? args[0] :
                    (args[0] && args[0].url) ? args[0].url : '';

        if (url.includes('/api/v0/chat/completion')) {
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

    return 'hooked_v8_xhr+fetch';
}
"""

POLL_STATE_JS = """() => ({
    finished: !!window.__ds_finished,
    textLen: window.__ds_lastTextLen || 0,
    source: window.__ds_hookSource || ''
})"""


async def stream_chat_gen(page, send_coro, round_num, idle_timeout=3):
    """异步生成器：yield (kind, data)
    kind ∈ {"body", "think", "done"}
    "done" 的 data 为 {"body": str, "think": str, "reason": str}
    """
    cdp = await page.context.new_cdp_session(page)
    chunk_queue = asyncio.Queue()
    live_parts = []
    think_parts = []
    chunk_count = 0
    think_count = 0
    finished_by_signal = False

    await cdp.send("Runtime.enable")
    await cdp.send("Runtime.addBinding", {"name": "__dsChunk"})
    await cdp.send("Runtime.addBinding", {"name": "__dsThink"})
    await cdp.send("Runtime.addBinding", {"name": "__dsDone"})

    def on_binding(params):
        name = params.get("name", "")
        payload = params.get("payload", "")
        if name == "__dsChunk":
            chunk_queue.put_nowait(("body", payload))
        elif name == "__dsThink":
            chunk_queue.put_nowait(("think", payload))
        elif name == "__dsDone":
            chunk_queue.put_nowait(("done", None))

    cdp.on("Runtime.bindingCalled", on_binding)

    hook_result = await page.evaluate(HOOK_JS_V8)
    await page.evaluate(f"""
        window.__ds_strip_citations = {str(STRIP_CITATIONS).lower()};
        window.__ds_finished = false;
        window.__ds_lastTextLen = 0;
        window.__ds_hookSource = '';
    """)
    log(f"Hook: {hook_result}")

    try:
        await send_coro
        log("开始接收流式数据...")

        ctx = gwc.new_wait_ctx()
        hook_source_logged = False

        while True:
            try:
                kind, data = await asyncio.wait_for(chunk_queue.get(), timeout=0.2)
                ctx["last_active"] = time.time()

                if kind == "done":
                    finished_by_signal = True
                    while not chunk_queue.empty():
                        try:
                            k2, d2 = chunk_queue.get_nowait()
                            if k2 == "body" and d2:
                                live_parts.append(d2)
                                yield ("body", d2)
                            elif k2 == "think" and d2:
                                think_parts.append(d2)
                                yield ("think", d2)
                        except asyncio.QueueEmpty:
                            break
                    break

                elif kind == "think":
                    think_count += 1
                    think_parts.append(data)
                    if data:
                        yield ("think", data)

                elif kind == "body":
                    chunk_count += 1
                    if data:
                        live_parts.append(data)
                        yield ("body", data)

            except asyncio.TimeoutError:
                cur_len = ctx["last_text_len"]
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
                                    live_parts.append(d2)
                                    yield ("body", d2)
                                elif k2 == "think" and d2:
                                    think_parts.append(d2)
                                    yield ("think", d2)
                            except asyncio.QueueEmpty:
                                break
                        break

                except Exception as poll_err:
                    log(f"⚠️ 轮询异常: {poll_err}")

                # 统一空闲判定(含硬上限, 修掉无限等待)。DeepSeek 无验证码 -> captcha_present=False
                action, reason = gwc.idle_decision(
                    ctx, False, cur_len, bool(live_parts), idle_timeout,
                    overall_timeout=STREAM_OVERALL_TIMEOUT,
                    first_token_timeout=FIRST_TOKEN_TIMEOUT,
                )
                if action == "break":
                    log(f"⚠️ {reason}")
                    break

        final_body = "".join(live_parts)
        final_think = "".join(think_parts)
        end_reason = "FINISHED信号" if finished_by_signal else "超时兜底"
        log(f"📊 正文chunk={chunk_count}/{len(final_body)}字符, 思考chunk={think_count}/{len(final_think)}字符, 结束方式={end_reason}")

        if DUMP_RAW:
            RAW_DUMP_DIR.mkdir(exist_ok=True)
            f = RAW_DUMP_DIR / f"round{round_num}_stream.txt"
            content = ""
            if final_think:
                content += f"=== 思考过程 ===\n{final_think}\n\n"
            content += f"=== 正文 ===\n{final_body}"
            f.write_text(content, encoding="utf-8")
            log(f"📄 已保存: {f.resolve()}")

        yield ("done", {"body": final_body, "think": final_think, "reason": end_reason,
                         "chunk_count": chunk_count, "think_count": think_count})

    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


async def stream_chat(page, send_coro, round_num, idle_timeout=3):
    """终端模式薄封装：调用 stream_chat_gen 并打印输出"""
    stream_prefix_printed = False
    think_prefix_printed = False
    final_body = ""
    final_think = ""
    chunk_count = 0

    async for kind, data in stream_chat_gen(page, send_coro, round_num, idle_timeout):
        if kind == "body":
            if think_prefix_printed and not stream_prefix_printed:
                print("\033[0m")
                think_prefix_printed = False
            if PRINT_STREAM_PREFIX and not stream_prefix_printed:
                print("AI: ", end="", flush=True)
                stream_prefix_printed = True
            print(data, end="", flush=True)
            final_body += data
            chunk_count += 1
        elif kind == "think":
            if STREAM_OUTPUT and data:
                if not think_prefix_printed:
                    print("\033[90m💭 ", end="", flush=True)
                    think_prefix_printed = True
                print(data, end="", flush=True)
            final_think += data
        elif kind == "done":
            final_body = data.get("body", final_body)
            final_think = data.get("think", final_think)
            chunk_count = data.get("chunk_count", chunk_count)

    if think_prefix_printed:
        print("\033[0m")
    if stream_prefix_printed:
        print()

    bar = "█" * 60
    log(bar)
    log(f"✅ 第 {round_num} 轮完成（正文 {len(final_body)} 字符）")
    log(bar)
    return chunk_count, len(final_body), final_body


async def do_send(page, query: str):
    """发送消息到 DeepSeek 输入框并提交（终端与 API 共用）"""
    ie = await find_element(page, INPUT_SELECTORS, "输入框")
    se = await find_element(page, SEND_SELECTORS, "发送按钮")
    if not ie:
        log("❌ 输入框丢失")
        return
    await ie.fill(query)
    if se:
        await se.click()
    else:
        await ie.press("Enter")
    log("已发送")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    other_chars = len(text) - chinese_chars - sum(len(w) for w in re.findall(r'[a-zA-Z]+', text))
    return max(1, int(chinese_chars * 1.5 + english_words * 1.3 + other_chars * 0.5))


# ============ \u4f1a\u8bdd\u6458\u8981 / \u538b\u7f29(\u4e0e qwen \u5bf9\u9f50) ============

async def _collect_web_reply(page, prompt_text, round_num):
    """\u5728\u5f53\u524d(\u5df2\u65b0\u5efa\u7684)\u4f1a\u8bdd\u91cc\u53d1\u9001 prompt_text \u5e76\u5b8c\u6574\u6536\u96c6\u56de\u590d, \u8fd4\u56de (body, think)\u3002\u7528\u4e8e\u6458\u8981\u3002"""
    final_body = ""
    final_think = ""
    async for kind, data in stream_chat_gen(page, do_send(page, prompt_text), round_num):
        if kind == "body":
            final_body += data
        elif kind == "think":
            final_think += data
        elif kind == "done":
            final_body = data.get("body", final_body)
            final_think = data.get("think", final_think)
    return final_body, final_think


async def maybe_compact(page, cid, round_num):
    """\u4f1a\u8bdd\u7d2f\u8ba1\u4f30\u7b97 token \u8d85\u9608\u503c\u65f6\u538b\u7f29: \u53e6\u5f00\u5e72\u51c0\u4f1a\u8bdd\u8ba9\u6a21\u578b\u628a[\u65e7\u6458\u8981+\u88ab\u6298\u53e0\u539f\u6587]\u538b\u6210\u65b0\u6458\u8981,
    \u53ea\u4fdd\u7559\u6700\u8fd1 K \u6761\u539f\u6587\u3002\u6458\u8981\u5931\u8d25\u5219\u9000\u5316\u4e3a"\u4fdd\u7559\u65e7\u6458\u8981 + \u6700\u8fd1 K \u6761"\u3002\u8fd4\u56de\u662f\u5426\u53d1\u751f\u538b\u7f29\u3002
    """
    state = session_store.load(SESSION_DB_PATH, cid)
    if state["total_tokens"] <= COMPACT_SOFT_LIMIT:
        return False

    turns = state["turns"]
    keep = turns[-COMPACT_KEEP_RECENT:] if COMPACT_KEEP_RECENT > 0 else []
    fold = turns[:len(turns) - len(keep)]

    conv_parts = []
    if state["summary"]:
        conv_parts.append("\u6b64\u524d\u6458\u8981:\n" + state["summary"])
    if fold:
        conv_parts.append(gwc.render_turns(fold))
    conversation_text = "\n\n".join(conv_parts).strip()
    if not conversation_text:
        return False

    log(f"\ud83e\uddec \u89e6\u53d1\u4f1a\u8bdd\u538b\u7f29 cid={cid} (\u7d2f\u8ba1~{state['total_tokens']} tokens)")
    prompt = COMPACT_PROMPT_TEMPLATE.format(
        max_chars=COMPACT_SUMMARY_MAX_CHARS, conversation=conversation_text
    )
    try:
        page2 = await start_new_chat(page)
        _app_state["page"] = page2
        body, _think = await _collect_web_reply(page2, prompt, round_num)
        new_summary = (body or "").strip()
        if not new_summary:
            raise RuntimeError("\u6458\u8981\u4e3a\u7a7a")
        session_store.replace_after_compaction(
            SESSION_DB_PATH, cid, new_summary, estimate_tokens(new_summary), keep
        )
        log(f"\ud83e\uddec \u538b\u7f29\u5b8c\u6210: \u6458\u8981 {len(new_summary)} \u5b57, \u4fdd\u7559\u6700\u8fd1 {len(keep)} \u6761\u539f\u6587")
        return True
    except Exception as e:
        log(f"\u26a0\ufe0f \u538b\u7f29\u5931\u8d25({type(e).__name__}: {e}), \u9000\u5316\u4e3a\u4fdd\u7559\u6700\u8fd1 {len(keep)} \u6761")
        session_store.replace_after_compaction(
            SESSION_DB_PATH, cid, state["summary"], state["summary_tokens"], keep
        )
        return True


# ============================================================
# FastAPI 应用（OpenAI 兼容）
# ============================================================
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

API_HOST = "0.0.0.0"
API_PORT = 8765
API_KEY = ""

SUPPORTED_MODELS = [
    {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
    {"id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek"},
]

_app_state: dict = {"page": None, "browser": None, "lock": None, "round_counter": 0}

# ============ 会话网关化 / 摘要压缩 / 超时中止(与 qwen 对齐, 共享同一 SQLite) ============
SESSION_DB_PATH = str(Path(__file__).resolve().parent / "sessions.db")  # 与 qwen 同一库, 跨进程共享会话
DEFAULT_CONVERSATION_ID = "default"   # 客户端不传 conversation_id 时的兜底会话
COMPACT_SOFT_LIMIT = 24000            # 会话累计估算 token 超过此值触发压缩
COMPACT_KEEP_RECENT = 3               # 压缩时保留最近轮数(user+assistant 计为多条)
COMPACT_SUMMARY_MAX_CHARS = 300       # 摘要长度约束

STREAM_OVERALL_TIMEOUT = 180          # 单轮硬上限秒数
FIRST_TOKEN_TIMEOUT = 60              # 无验证时等待首个回复 token 的上限秒数
# DeepSeek 无滑块验证: idle_decision 走 allow_restart=False、captcha_present=False 分支

CONTEXT_HEADER = (
    "以下 <wxg_summary> 是此前对话摘要, <wxg_history> 是最近若干轮原文(每个 <wxg_turn> 含 n=轮次、role=角色), "
    "<wxg_current> 是我当前的问题。请在此背景上继续回答, 不要复述背景本身。"
)
COMPACT_PROMPT_TEMPLATE = (
    "请把下面这段多轮对话压缩成一份简洁摘要, 只保留后续继续对话所必需的信息: "
    "关键事实、已达成的结论、尚未解决的问题、重要前提与用户偏好。"
    "用要点列出, 不要展开寒暄与客套, 不超过{max_chars}字。只输出摘要本身, 不要额外说明。\n\n"
    "====== 对话开始 ======\n{conversation}\n====== 对话结束 ======"
)


def _check_api_key(request: Request) -> Optional[str]:
    if not API_KEY:
        return None
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if token == API_KEY:
            return None
    return "invalid_api_key"


def _build_chat_chunk(content: str = "", reasoning: str = "", model: str = "deepseek-chat",
                      chunk_id: str = "", finish: Optional[str] = None,
                      usage: Optional[dict] = None) -> dict:
    delta = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **({"usage": usage} if usage else {}),
    }


def _build_chat_response(content: str, reasoning: str, model: str, chat_id: str,
                         usage: Optional[dict] = None) -> dict:
    if usage is None:
        prompt_tokens = estimate_tokens(reasoning) + estimate_tokens(content)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": estimate_tokens(content),
            "total_tokens": prompt_tokens + estimate_tokens(content),
        }
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                **({"reasoning_content": reasoning} if reasoning else {}),
            },
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


def create_app():
    app = FastAPI(title="DeepSeek Web Hook API", version="1.0")

    @app.get("/")
    async def root():
        return {"status": "ok", "service": "deepseek-web-api"}

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "deepseek-web-api"}

    @app.get("/v1/models")
    async def list_models():
        return {"object": "list", "data": SUPPORTED_MODELS}

    @app.get("/v1/modes")
    async def list_modes():
        return {"object": "list", "data": [
            {"id": "default", "name": "快速模式"},
            {"id": "expert", "name": "专家模式"},
            {"id": "vision", "name": "识图模式"},
        ]}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        err = _check_api_key(request)
        if err:
            return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key", "type": err}})

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON"}})

        messages = body.get("messages", [])
        if not messages:
            return JSONResponse(status_code=400, content={"error": {"message": "messages is required"}})

        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        if not user_msg:
            return JSONResponse(status_code=400, content={"error": {"message": "No user message found"}})

        model = body.get("model", "deepseek-chat")
        stream = body.get("stream", False)
        req_mode = body.get("mode", "")  # "expert" / "vision" / "default"
        # 会话正本由网关持有: conversation_id 标识调用方逻辑会话(与 qwen 共享同一库)
        cid = body.get("conversation_id") or DEFAULT_CONVERSATION_ID
        new_session = body.get("new_session", False)
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        page = _app_state["page"]
        lock = _app_state["lock"]
        if page is None or lock is None:
            return JSONResponse(status_code=503, content={"error": {"message": "Browser not ready"}})

        # new_session=true 语义: 清空该 cid 历史
        if new_session:
            session_store.clear(SESSION_DB_PATH, cid)
            log(f"🔄 API 清空会话历史 cid={cid}")

        # 取历史正本, 拼装[摘要+最近K轮+本轮]
        state = session_store.load(SESSION_DB_PATH, cid)
        injected = gwc.assemble_context(state, user_msg, CONTEXT_HEADER)

        _app_state["round_counter"] += 1
        round_num = _app_state["round_counter"]
        log(f"===== 第 {round_num} 轮 (API) cid={cid} 注入~{estimate_tokens(injected)} tokens =====")

        mode_to_label = {"default": "快速模式", "expert": "专家模式", "vision": "识图模式"}

        async def _ensure_mode(p):
            if not req_mode:
                return
            label = mode_to_label.get(req_mode, req_mode)
            await switch_chat_mode(p, label)

        def _persist_and_usage(final_body, final_think):
            """落库(user+assistant)并返回 usage; 无有效回复则不落库, 避免污染上下文。"""
            if final_body and final_body.strip():
                try:
                    session_store.append_turn(SESSION_DB_PATH, cid, "user", user_msg, estimate_tokens(user_msg))
                    session_store.append_turn(SESSION_DB_PATH, cid, "assistant", final_body, estimate_tokens(final_body))
                except Exception as e:
                    log(f"⚠️ 历史落库失败: {type(e).__name__}: {e}")
            else:
                log("⚠️ 本轮无有效回复, 跳过历史落库")
            prompt_tok = estimate_tokens(injected)
            body_tok = estimate_tokens(final_body)
            think_tok = estimate_tokens(final_think)
            return {
                "prompt_tokens": prompt_tok,
                "completion_tokens": body_tok,
                "reasoning_tokens": think_tok,
                "total_tokens": prompt_tok + body_tok + think_tok,
                "conversation_id": cid,
            }

        # ========= 流式响应 =========
        async def stream_generator():
            final_body = ""
            final_think = ""
            async with lock:
                # 先开干净会话(导航回首页), 再切模式, 最后注入拼好的上下文
                p = await start_new_chat(page)
                _app_state["page"] = p
                await _ensure_mode(p)
                try:
                    async for kind, data in stream_chat_gen(p, do_send(p, injected), round_num):
                        if kind == "body":
                            final_body += data
                            yield f"data: {json.dumps(_build_chat_chunk(content=data, model=model, chunk_id=chat_id), ensure_ascii=False)}\n\n"
                        elif kind == "think":
                            final_think += data
                            yield f"data: {json.dumps(_build_chat_chunk(reasoning=data, model=model, chunk_id=chat_id), ensure_ascii=False)}\n\n"
                        elif kind == "done":
                            final_body = data.get("body", final_body)
                            final_think = data.get("think", final_think)
                except Exception as e:
                    log(f"❌ API 流式异常: {type(e).__name__}: {e}")
                    yield f"data: {json.dumps(_build_chat_chunk(content=f'[error] {e}', model=model, chunk_id=chat_id), ensure_ascii=False)}\n\n"

                usage = _persist_and_usage(final_body, final_think)
                compacted = False
                try:
                    compacted = await maybe_compact(_app_state["page"], cid, _app_state["round_counter"])
                except Exception as e:
                    log(f"⚠️ 压缩异常: {type(e).__name__}: {e}")
                usage["compacted"] = compacted
                usage["session_tokens"] = session_store.load(SESSION_DB_PATH, cid)["total_tokens"]
                yield f"data: {json.dumps(_build_chat_chunk(model=model, chunk_id=chat_id, finish='stop', usage=usage), ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        if stream:
            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        final_body = ""
        final_think = ""
        async with lock:
            try:
                p = await start_new_chat(page)
                _app_state["page"] = p
                await _ensure_mode(p)
                async for kind, data in stream_chat_gen(p, do_send(p, injected), round_num):
                    if kind == "body":
                        final_body += data
                    elif kind == "think":
                        final_think += data
                    elif kind == "done":
                        final_body = data.get("body", final_body)
                        final_think = data.get("think", final_think)
            except Exception as e:
                log(f"❌ API 非流式异常: {type(e).__name__}: {e}")
                return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

        usage = _persist_and_usage(final_body, final_think)
        compacted = False
        try:
            compacted = await maybe_compact(_app_state["page"], cid, _app_state["round_counter"])
        except Exception as e:
            log(f"⚠️ 压缩异常: {type(e).__name__}: {e}")
        usage["compacted"] = compacted
        usage["session_tokens"] = session_store.load(SESSION_DB_PATH, cid)["total_tokens"]
        return _build_chat_response(final_body, final_think, model, chat_id, usage=usage)

    @app.post("/v1/session/reset")
    async def session_reset(request: Request):
        err = _check_api_key(request)
        if err:
            return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key", "type": err}})
        cid = DEFAULT_CONVERSATION_ID
        try:
            rbody = await request.json()
            if isinstance(rbody, dict) and rbody.get("conversation_id"):
                cid = rbody["conversation_id"]
        except Exception:
            pass
        session_store.clear(SESSION_DB_PATH, cid)
        log(f"🔄 已清空会话历史 cid={cid}")
        return {"status": "ok", "message": "Session cleared", "conversation_id": cid}

    return app


async def run_api_server(host: str, port: int):
    import uvicorn

    log("脚本启动（API 模式）")

    # 启动前检测端口是否已被任一 IP 占用(含 127.0.0.1 / ::1 / 通配), 占用即报错退出。
    # 避免"打印了成功横幅但请求其实被别的服务(如抢 127.0.0.1:8000 的 Beem H)截走"的误导。
    occupied, why = gwc.port_in_use(port)
    if occupied:
        log(f"❌ 端口 {port} 已被占用: {why}")
        log(f"   请换端口启动 `--port 8001`, 或先停掉占用进程(查: lsof -nP -iTCP:{port} -sTCP:LISTEN)。")
        return
    log(f"✅ 端口 {port} 未被占用")

    # 初始化会话历史存储(与 qwen 共享同一 SQLite, 跨进程共享同一 conversation_id)
    try:
        session_store.init(SESSION_DB_PATH)
        log(f"🗄️  会话历史库: {SESSION_DB_PATH} (与 qwen 共享)")
    except Exception as e:
        log(f"⚠️ 会话历史库初始化失败: {type(e).__name__}: {e}")
    chrome_proc = launch_chrome()
    attached = (chrome_proc is None)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        log("CDP 连接成功")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page, is_reused = await find_or_create_deepseek_page(context)

        if not is_reused:
            await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded")
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
        if DEFAULT_CHAT_MODE:
            await switch_chat_mode(page, DEFAULT_CHAT_MODE)

        _app_state["page"] = page
        _app_state["browser"] = browser
        _app_state["lock"] = asyncio.Lock()

        mode = "附加" if attached else "新启动"
        tab = "复用" if is_reused else "新建"
        log(f"✅ 就绪（{mode}/{tab}）")
        log(f"🚀 API 服务启动: http://{host}:{port}")
        log(f"   POST /v1/chat/completions  - 聊天(每轮新建会话+注入历史)")
        log(f"   POST /v1/session/reset    - 清空会话历史(可带 conversation_id)")
        log(f"   GET  /v1/models / /v1/modes")
        log(f"   参数 conversation_id 区分逻辑会话(与 qwen 共享); new_session=true 清空")
        log(f"   会话正本存于 {SESSION_DB_PATH}")
        if API_KEY:
            log(f"   API Key: {API_KEY}")
        log(f"   (Ctrl+C 退出)\n")

        log("   (下方 uvicorn 'Uvicorn running on ...' 才是真正绑定成功的权威标志;")
        log("    每次请求会打一行 access 日志, 若 curl 时此处无新日志=请求没打到本服务)")
        app = create_app()
        # info + access_log: 打印真实绑定地址与每次请求, 便于确认"监听端口的服务正常"
        config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=True)
        server = uvicorn.Server(config)

        try:
            await server.serve()
        finally:
            await browser.close()
            log("退出")


async def main():
    global API_KEY
    parser = argparse.ArgumentParser(description="DeepSeek Web Hook")
    parser.add_argument("--api", action="store_true", help="启动 OpenAI 兼容 API 服务")
    parser.add_argument("--host", default=API_HOST, help=f"API 监听地址（默认 {API_HOST}）")
    parser.add_argument("--port", type=int, default=API_PORT, help=f"API 监听端口（默认 {API_PORT}）")
    parser.add_argument("--api-key", default=API_KEY, help="API 鉴权密钥（留空则不鉴权）")
    args = parser.parse_args()

    if args.api:
        API_KEY = args.api_key
        await run_api_server(args.host, args.port)
        return

    log("脚本启动")
    chrome_proc = launch_chrome()
    attached = (chrome_proc is None)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        log("CDP 连接成功")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page, is_reused = await find_or_create_deepseek_page(context)

        if not is_reused:
            await page.goto(DEEPSEEK_URL, wait_until="domcontentloaded")
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
        if DEFAULT_CHAT_MODE:
            await switch_chat_mode(page, DEFAULT_CHAT_MODE)

        mode = "附加" if attached else "新启动"
        tab = "复用" if is_reused else "新建"
        log(f"✅ 就绪（{mode}/{tab}）\n")

        round_num = 0
        while True:
            raw = await prompt_query("You: ", COMMANDS)
            if raw is None:
                break
            query = normalize_command(raw, COMMANDS)
            if not query or query.lower() == "quit":
                break
            if query.lower() == "help":
                print_help(COMMANDS, "DeepSeek 交互命令")
                continue
            if query.lower() == "chatmodes":
                await list_chat_modes(page)
                continue
            if query.lower().startswith("mode "):
                mn = query.strip()[5:].strip()
                await switch_chat_mode(page, mn)
                continue
            if query.lower() == "mode":
                log("用法: /mode <模式名>  (快速模式 / 专家模式 / 识图模式)")
                continue

            round_num += 1
            log(f"===== 第 {round_num} 轮 =====")

            try:
                await stream_chat(page, do_send(page, query), round_num)
            except Exception as e:
                log(f"❌ {type(e).__name__}: {e}")

            log(f"===== 第 {round_num} 轮结束 =====\n")

        await browser.close()
        log("退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
