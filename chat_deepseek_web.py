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
DEFAULT_CHAT_MODE = "专家模式"  # "专家模式" 或 "识图模式"，留空保持默认
# ===============================


# run.log 文件句柄（追加模式，由 start_deepseek.sh 在启动时清空，保证 run.log 始终是本次启动期间的日志）
RUN_LOG_PATH = Path(__file__).parent / "run.log"
_run_log_file = open(RUN_LOG_PATH, "a", encoding="utf-8")


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S") + f",{int(time.time() * 1000) % 1000:03d}"
    line = f"[执行日志] {ts} {msg}"
    print(line, flush=True)
    try:
        _run_log_file.write(line + "\n")
        _run_log_file.flush()
    except Exception:
        pass


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


async def ensure_modes_off(page, mode_names):
    """把指定模式确保切到【关闭】(仅当前为开启时才点一下)。
    工具模式下用于关掉「深度思考」「智能搜索」, 让模型输出更干脆、更听格式约定。"""
    log(f"🎛️  关闭模式: {mode_names}")
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
                continue
            handle = await chip.element_handle()
            if await page.evaluate(check_js, handle):
                await chip.click()
                await page.wait_for_timeout(500)
                log(f"   🔘 已关闭「{name}」")
            else:
                log(f"   ✅ 「{name}」本就关闭")
        except Exception as e:
            log(f"   ❌ 关闭「{name}」出错: {e}")


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


# ============================================================
# 清空全部历史会话（模拟人类点击：展开侧边栏 → 多选 → 全选 → 删除 → 确认）
# 说明：第一版带大量调试输出(按钮dump+截图)，便于按真实DOM固化选择器
# ============================================================
# ---- 人性化点击(随机延迟+鼠标轨迹, 降低被判定为机器人的风险) ----
async def _human_delay(a=0.3, b=0.9):
    try:
        await asyncio.sleep(random.uniform(a, b))
    except Exception:
        pass


async def _human_click_xy(page, x, y):
    try:
        await page.mouse.move(x, y, steps=random.randint(6, 16))
        await _human_delay(0.12, 0.35)
        await page.mouse.click(x, y, delay=random.randint(40, 130))
    except Exception as e:
        log(f"   ⚠️ 人性化点击异常: {e}")
    await _human_delay(0.5, 1.5)


async def _move_pause_click(page, x, y, pause=0.5):
    """移动到坐标→停顿→点击(更像人对准按钮再按)。"""
    try:
        await page.mouse.move(x, y, steps=random.randint(8, 18))
        await asyncio.sleep(pause + random.uniform(0.0, 0.3))
        await page.mouse.click(x, y, delay=random.randint(50, 120))
    except Exception as e:
        log(f"   ⚠️ 移动停顿点击异常: {e}")


async def _human_click(page, locator):
    box = None
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if box:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await _human_click_xy(page, x, y)
        return True
    try:
        await locator.click()
        await _human_delay()
        return True
    except Exception as e:
        log(f"   ⚠️ 点击异常: {e}")
        return False


async def _save_debug_shot(page, name):
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        d = Path("./debug_screenshots")
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"clearhist_{name}_{ts}.png"
        await page.screenshot(path=str(path))
        log(f"   📸 截图已存: {path}")
    except Exception as e:
        log(f"   ⚠️ 截图失败: {e}")


async def _dump_buttons(page, label):
    try:
        infos = await page.evaluate(r"""() => {
            const out = [];
            const els = document.querySelectorAll('button,[role="button"],[role="checkbox"],[role="radio"],[role="menuitem"]');
            els.forEach((b,i) => { if (i<150) out.push({
                tag: b.tagName,
                role: b.getAttribute('role'),
                al: b.getAttribute('aria-label'),
                ac: b.getAttribute('aria-checked'),
                t: (b.innerText||'').replace(/\s+/g,' ').trim().slice(0,24),
                cls: (b.getAttribute('class')||'').slice(0,60)
            }); });
            return out;
        }""")
        log(f"   🧩 [{label}] 可点击元素({len(infos)}):")
        for it in infos:
            log(f"       {it}")
    except Exception as e:
        log(f"   ⚠️ dump buttons[{label}] 失败: {e}")


async def _sidebar_visible(page):
    try:
        nc = page.get_by_text("开启新对话", exact=False)
        if await nc.count() > 0 and await nc.first.is_visible():
            return True
    except Exception:
        pass
    for t in ["今天", "昨天", "30 天内", "30天内", "7 天内"]:
        try:
            e = page.get_by_text(t, exact=False)
            if await e.count() > 0 and await e.first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _ensure_sidebar_open(page):
    if await _sidebar_visible(page):
        log("   ✅ 侧边栏已展开")
        return True
    log("   ▶ 侧边栏未展开, 点击左上角侧边栏开关...")
    # DeepSeek 顶部图标按钮(div.ds-button--icon)无 aria-label/文本,
    # 侧边栏开关是顶部区域最靠左的那个图标按钮
    for attempt in range(3):
        try:
            await page.evaluate(r"""() => {
                const btns = Array.from(document.querySelectorAll('.ds-button--icon'))
                    .filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top < 150; });
                if (!btns.length) return false;
                btns.sort((a,b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                btns[0].click();
                return true;
            }""")
        except Exception as e:
            log(f"   ⚠️ 点击侧边栏开关异常: {e}")
        await page.wait_for_timeout(900)
        if await _sidebar_visible(page):
            log(f"   ✅ 侧边栏已展开(左上角图标, 第{attempt+1}次)")
            return True
    log("   ⚠️ 未能展开侧边栏, dump+截图供排查")
    await _dump_buttons(page, "sidebar-toggle")
    await _save_debug_shot(page, "sidebar")
    return await _sidebar_visible(page)


async def _in_multiselect(page):
    for t in ["选择对话", "已选择"]:
        try:
            e = page.get_by_text(t, exact=False)
            if await e.count() > 0 and await e.first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _open_multiselect(page):
    if await _in_multiselect(page):
        return True
    # 语义定位: 侧边栏第一个分组标题(今天/昨天/N天内/YYYY-MM)行内、右侧的图标按钮
    res = await page.evaluate(r"""() => {
        const nodes = Array.from(document.querySelectorAll('div,span,p'));
        let header = null;
        for (const e of nodes) {
            if (e.children.length) continue;
            const t = (e.textContent || '').trim();
            if (/^(今天|昨天|前天|近\d+天|\d+\s*天内|\d{4}-\d{2})$/.test(t)) {
                const r = e.getBoundingClientRect();
                if (r.top > 0 && r.left < window.innerWidth * 0.4) { header = e; break; }
            }
        }
        if (!header) return {err: 'no-header'};
        const hr = header.getBoundingClientRect();
        const hcy = hr.top + hr.height / 2;
        const icons = Array.from(document.querySelectorAll('.ds-button--icon')).filter(b => {
            const r = b.getBoundingClientRect();
            return r.width > 0 && Math.abs((r.top + r.height / 2) - hcy) < 26 && r.left > hr.left;
        });
        if (!icons.length) return {err: 'no-icon-in-row', headerText: header.textContent.trim(), headerTop: Math.round(hr.top)};
        icons.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
        const r = icons[0].getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2, headerText: header.textContent.trim()};
    }""")
    log(f"   🔎 多选按钮定位结果: {res}")
    if isinstance(res, dict) and "x" in res:
        await _human_click_xy(page, res["x"], res["y"])
        await page.wait_for_timeout(700)
        if await _in_multiselect(page):
            log("   ✅ 已进入多选态(分组标题行图标)")
            return True
    log("   ⚠️ 未能进入多选态, dump 按钮供排查")
    await _dump_buttons(page, "multiselect-entry")
    await _save_debug_shot(page, "multiselect_entry")
    return False


async def _select_all_history(page):
    """多选态下, 逐个勾选未选中的会话行(点行内勾选区, 靠 <a> 的 aria-pressed 判断选中)。
    每轮最多勾 10 项; 每次重新定位第一个未选中行, 避免重复点击把已选的取消。"""
    clicked = 0
    for _ in range(50):
        box = await page.evaluate(r"""() => {
            const rows = Array.from(document.querySelectorAll('a[aria-pressed]'));
            for (const a of rows) {
                if (a.getAttribute('aria-pressed') === 'true') continue;
                const r = a.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                if (r.top < 60 || r.bottom > window.innerHeight - 40) continue;
                const cb = a.querySelector('[class*=checkbox]');
                const cr = cb ? cb.getBoundingClientRect() : r;
                return {x: cr.x + cr.width / 2, y: cr.y + cr.height / 2, text: (a.innerText || '').replace(/\s+/g, ' ').slice(0, 20)};
            }
            return null;
        }""")
        if not box:
            break
        try:
            await page.mouse.move(box["x"], box["y"], steps=random.randint(3, 7))
            await page.mouse.click(box["x"], box["y"], delay=random.randint(30, 80))
        except Exception:
            pass
        await _human_delay(0.15, 0.3)
        clicked += 1
    log(f"   ✅ 本轮勾选 {clicked} 项")
    return clicked


async def _delete_dialog_visible(page):
    for t in ["删除选择的", "不可恢复", "不可恢復"]:
        try:
            e = page.get_by_text(t, exact=False)
            if await e.count() > 0 and await e.first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _click_delete_toolbar(page):
    """鼠标移到底部工具栏'删除'→停0.5s→点击, 触发确认弹窗。"""
    box = await page.evaluate(r"""() => {
        const btns = Array.from(document.querySelectorAll('button')).filter(b => {
            const t = (b.innerText || '').trim();
            const r = b.getBoundingClientRect();
            return t === '删除' && r.width > 0 && r.height > 0;
        });
        if (!btns.length) return null;
        btns.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
        const r = btns[0].getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2, n: btns.length};
    }""")
    log(f"   🗑️ 工具栏删除按钮定位: {box}")
    if box:
        await _move_pause_click(page, box["x"], box["y"], pause=0.5)
        for _ in range(8):
            if await _delete_dialog_visible(page):
                log("   ✅ 已点击工具栏删除, 确认弹窗已出现")
                return True
            await page.wait_for_timeout(350)
    log("   ⚠️ 未能点击工具栏删除或未弹出确认框")
    await _dump_buttons(page, "delete-toolbar")
    await _save_debug_shot(page, "delete_toolbar")
    return False


async def _confirm_delete(page):
    """在确认弹窗内定位红色'删除'(可能是 div.ds-button), 鼠标移过去→停0.5s→点击→等2秒完成删除。"""
    box = await page.evaluate(r"""() => {
        const marker = Array.from(document.querySelectorAll('*')).find(e => {
            const t = (e.textContent || '');
            return t.includes('不可恢复') || t.includes('删除选择的');
        });
        let dlg = marker;
        for (let i = 0; i < 8 && dlg; i++) {
            const c = dlg.querySelectorAll('button,[role=button],.ds-button');
            if (c.length >= 2) break;
            dlg = dlg.parentElement;
        }
        const scope = dlg || document;
        const cbtns = Array.from(scope.querySelectorAll('button,[role=button],.ds-button')).filter(b => {
            const t = (b.innerText || b.textContent || '').trim();
            const r = b.getBoundingClientRect();
            return t === '删除' && r.width > 0 && r.height > 0;
        });
        if (!cbtns.length) {
            const dbg = Array.from(scope.querySelectorAll('button,[role=button],.ds-button')).map(b => ({t: (b.innerText || b.textContent || '').trim().slice(0, 10), tag: b.tagName})).slice(0, 8);
            return {err: 'no-del-btn', scoped: !!dlg, dbg: dbg};
        }
        cbtns.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
        const r = cbtns[0].getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2, n: cbtns.length, scoped: !!dlg};
    }""")
    log(f"   ✔️ 弹窗删除按钮定位: {box}")
    if isinstance(box, dict) and "x" in box:
        await _move_pause_click(page, box["x"], box["y"], pause=0.5)
        await page.wait_for_timeout(2000)
        log("   ✅ 已在弹窗确认删除(已等待2秒)")
        return True
    log("   ⚠️ 未能定位弹窗删除按钮")
    await _dump_buttons(page, "confirm-dialog")
    await _save_debug_shot(page, "confirm_dialog")
    return False


async def clear_all_history(page):
    """分批循环清空全部历史(应对虚拟滚动)。每轮: 回首页→展开→进多选→勾选可见→删除→确认。
    结束条件: 进不了多选态 或 本轮勾选 0 项(视为已无历史)。不再依赖单一 locator 计数做判据。"""
    log("🧹 开始清空全部历史会话(分批循环, 应对虚拟滚动)...")
    MAX_ROUNDS = 60
    for rnd in range(1, MAX_ROUNDS + 1):
        if not await _reset_to_home(page):
            log("   ⚠️ 无法回到首页, 中止")
            return False
        await page.wait_for_timeout(800)
        await _ensure_sidebar_open(page)
        # 等待侧栏列表渲染, 打印计数明细(仅供观察, 不作判据)
        # 等待侧栏列表渲染出来
        for _ in range(10):
            try:
                ready = await page.evaluate("() => document.querySelectorAll('a[aria-pressed], a[role=button]').length > 0")
            except Exception:
                ready = False
            if ready:
                break
            await page.wait_for_timeout(400)
        log(f"   —— 第 {rnd} 轮 ——")
        if not await _open_multiselect(page):
            log("   ℹ️ 未进入多选态(视为已无历史或需排查), 结束")
            await _save_debug_shot(page, "after")
            return True
        n = await _select_all_history(page)
        if n == 0:
            log("   ℹ️ 本轮勾选 0 项, 判定已清空, 结束")
            await _save_debug_shot(page, "after")
            return True
        if not await _click_delete_toolbar(page):
            log("   ❌ 点击删除失败, 中止")
            return False
        if not await _confirm_delete(page):
            log("   ❌ 确认删除失败, 中止")
            return False
        await page.wait_for_timeout(1300)
    log("🧹 历史会话清空流程结束(达到轮数上限)")
    await _save_debug_shot(page, "after")
    return True


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
    window.__ds_raw_sse = '';

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
            window.__ds_raw_sse = (window.__ds_raw_sse || '') + text;
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
        window.__ds_raw_sse = '';
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
            try:
                await page.wait_for_timeout(1000)  # 宽限: 捕获 FINISHED 之后可能仍到达的 SSE
                raw_sse = await page.evaluate("() => window.__ds_raw_sse || ''")
                rf = RAW_DUMP_DIR / f"round{round_num}_raw_sse.txt"
                rf.write_text(raw_sse, encoding="utf-8")
                log(f"📄 已保存原始SSE: {rf.name} ({len(raw_sse)} 字符)")
            except Exception as e:
                log(f"⚠️ 保存原始SSE失败: {type(e).__name__}: {e}")

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
                         usage: Optional[dict] = None, tool_calls: Optional[list] = None) -> dict:
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
                "content": (None if tool_calls else content),
                **({"reasoning_content": reasoning} if reasoning else {}),
                **({"tool_calls": tool_calls} if tool_calls else {}),
            },
            "finish_reason": ("tool_calls" if tool_calls else "stop"),
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
        # 兼容 OpenAI content-parts(list)格式: 归一化为字符串, 纯字符串保持不变
        if isinstance(user_msg, list):
            user_msg = "".join(
                part.get("text", "")
                for part in user_msg
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if not user_msg:
            return JSONResponse(status_code=400, content={"error": {"message": "No user message found"}})

        model = body.get("model", "deepseek-chat")
        stream = body.get("stream", False)
        req_mode = body.get("mode", "")  # "expert" / "vision" / "default"
        # 会话正本由网关持有: conversation_id 标识调用方逻辑会话(与 qwen 共享同一库)
        cid = body.get("conversation_id") or DEFAULT_CONVERSATION_ID
        new_session = body.get("new_session", False)
        tools = body.get("tools")
        use_tools = bool(tools)
        tool_nonce = uuid.uuid4().hex[:8] if use_tools else ""
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        page = _app_state["page"]
        lock = _app_state["lock"]
        if page is None or lock is None:
            return JSONResponse(status_code=503, content={"error": {"message": "Browser not ready"}})

        # new_session=true 语义: 清空该 cid 历史
        if new_session:
            session_store.clear(SESSION_DB_PATH, cid)
            log(f"🔄 API 清空会话历史 cid={cid}")

        # 工具模式(请求带 tools): 直接用 messages 渲染, 不走 SQLite 历史
        if use_tools:
            injected = gwc.render_messages_for_tools(messages, tools, tool_nonce)
        else:
            # 取历史正本, 拼装[摘要+最近K轮+本轮]
            state = session_store.load(SESSION_DB_PATH, cid)
            injected = gwc.assemble_context(state, user_msg, CONTEXT_HEADER)

        _app_state["round_counter"] += 1
        round_num = _app_state["round_counter"]
        log(f"===== 第 {round_num} 轮 (API) cid={cid} 注入~{estimate_tokens(injected)} tokens =====")
        log(f"   stream={stream}, use_tools={use_tools}")
        # 请求正文单独落盘(独立 req 文件, 不混入 run.log), 与 round{N}_stream.txt 响应配套排查
        if DUMP_RAW:
            try:
                RAW_DUMP_DIR.mkdir(exist_ok=True)
                (RAW_DUMP_DIR / f"round{round_num}_request.txt").write_text(injected, encoding="utf-8")
            except Exception as e:
                log(f"⚠️ 保存请求失败: {type(e).__name__}: {e}")

        mode_to_label = {"default": "快速模式", "expert": "专家模式", "vision": "识图模式"}

        async def _ensure_mode(p):
            # Trae 等不传 mode 时, 每轮回退到默认模式(DEFAULT_CHAT_MODE=专家模式), 防止新对话回落到快速模式
            label = mode_to_label.get(req_mode, req_mode) if req_mode else DEFAULT_CHAT_MODE
            if not label:
                return
            await switch_chat_mode(p, label)

        async def _apply_modes(p):
            # 统一模式策略: 每轮先切 chat 模式(专家模式), 再按是否工具调用设定思考/搜索开关
            await _ensure_mode(p)
            if use_tools:
                # 工具调用: 只用专家模式, 关深度思考(输出干脆)与智能搜索(不触发 DEEP_SEARCH 打开本地文件)
                await ensure_modes_off(p, ["深度思考", "智能搜索"])
            else:
                # 内容理解: 专家模式 + 深度思考; 智能搜索仍关闭(避免联网打开本地文件)
                await ensure_modes(p, ["深度思考"])
                await ensure_modes_off(p, ["智能搜索"])

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
                await _apply_modes(p)
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

        if stream and not use_tools:
            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        final_body = ""
        final_think = ""
        async with lock:
            try:
                p = await start_new_chat(page)
                _app_state["page"] = p
                await _apply_modes(p)
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

        # ===== 工具模式: 不落 SQLite / 不压缩; 只用正文解析 -> OpenAI tool_calls; 不回传思考 =====
        if use_tools:
            prompt_tok = estimate_tokens(injected)
            body_tok = estimate_tokens(final_body)
            usage = {
                "prompt_tokens": prompt_tok,
                "completion_tokens": body_tok,
                "total_tokens": prompt_tok + body_tok,
                "conversation_id": cid,
            }
            # 哨兵可能落在思考区(关深度思考后 body 常为空), 合并 think+body 一起解析; nonce 防误命中
            combined = ((final_think or "") + "\n" + (final_body or "")).strip()
            calls, note = gwc.parse_tool_calls(combined, tool_nonce)
            if calls:
                tool_calls = []
                for c in calls:
                    tool_calls.append({
                        "id": f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c.get("arguments", {}), ensure_ascii=False),
                        },
                    })
                names = ", ".join(t["function"]["name"] for t in tool_calls)
                log(f"🔧 解析到 {len(tool_calls)} 个工具调用({note}): {names}")
                if stream:
                    # 流式回放: 先发带 tool_calls 的 delta, 再发 finish=tool_calls
                    async def _toolcalls_sse():
                        delta_tcs = [
                            {"index": i, "id": t["id"], "type": "function",
                             "function": {"name": t["function"]["name"],
                                          "arguments": t["function"]["arguments"]}}
                            for i, t in enumerate(tool_calls)
                        ]
                        first = {
                            "id": chat_id, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": model,
                            "choices": [{"index": 0,
                                         "delta": {"role": "assistant", "content": None, "tool_calls": delta_tcs},
                                         "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps(_build_chat_chunk(model=model, chunk_id=chat_id, finish='tool_calls', usage=usage), ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                    return StreamingResponse(_toolcalls_sse(), media_type="text/event-stream")
                # 非流式: 只回传 tool_calls, 不含思考过程
                return _build_chat_response("", "", model, chat_id, usage=usage, tool_calls=tool_calls)
            # 未命中: 正文空则回退用思考区内容, 保证 Trae 不拿到空回复
            content_out = final_body if (final_body and final_body.strip()) else final_think
            log(f"⚠️ 工具模式未解析到调用({note}); combined[:200]={combined[:200]!r}")
            if stream:
                async def _content_sse():
                    if content_out:
                        yield f"data: {json.dumps(_build_chat_chunk(content=content_out, model=model, chunk_id=chat_id), ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps(_build_chat_chunk(model=model, chunk_id=chat_id, finish='stop', usage=usage), ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(_content_sse(), media_type="text/event-stream")
            return _build_chat_response(content_out, "", model, chat_id, usage=usage)

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


async def run_api_server(host: str, port: int, clear_history: bool = False, random_sessionid: bool = False):
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

    if random_sessionid:
        global DEFAULT_CONVERSATION_ID
        DEFAULT_CONVERSATION_ID = f"default-{uuid.uuid4().hex[:12]}"
        log(f"🆕 本次启动随机默认会话id: {DEFAULT_CONVERSATION_ID}")
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

        if clear_history:
            await clear_all_history(page)
            await start_new_chat(page)

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
    parser.add_argument("--clear-history", action="store_true", help="启动时先清空全部历史会话, 再开一个干净的新会话")
    parser.add_argument("--random-sessionid", action="store_true",
                        help="每次启动生成独立随机默认会话id, 不带conversation_id的请求纯新且互不串味")
    args = parser.parse_args()

    if args.api:
        API_KEY = args.api_key
        await run_api_server(args.host, args.port, args.clear_history, args.random_sessionid)
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

        if args.clear_history:
            await clear_all_history(page)
            await start_new_chat(page)

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
