# -*- coding: utf-8 -*-
"""浏览器启动、Chrome DevTools 端口探测、页面查找与登录等待。"""
import json
import socket
import subprocess
import time
import urllib.request

from . import config
from .state import log


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_chrome_debug_port(port: int) -> bool:
    """通过 HTTP 请求 /json/version 验证端口是否为有效的 Chrome DevTools 端点"""
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


def _is_chrome_headless() -> bool:
    """通过 CDP /json/version 检测现有 Chrome 是否为 headless 模式"""
    try:
        url = f"http://127.0.0.1:{config.DEBUG_PORT}/json/version"
        resp = urllib.request.urlopen(url, timeout=3)
        version_info = json.loads(resp.read())
        browser = version_info.get("Browser", "")
        return "HeadlessChrome" in browser
    except Exception:
        return False


def _kill_chrome_on_port(port: int):
    """杀掉占用指定端口的 Chrome 进程"""
    procs = find_port_processes(port)
    chrome_pids = [pid for pid, cmd in procs if "chrome" in cmd.lower()]
    if chrome_pids:
        log(f"🔧 清理 {len(chrome_pids)} 个 Chrome 进程 (PID: {chrome_pids})...")
        kill_processes(chrome_pids)


def launch_chrome():
    config.USER_DATA_DIR.mkdir(exist_ok=True)

    if is_chrome_usable(config.DEBUG_PORT):
        if not config.HEADLESS:
            # 有头模式: 永远不复用旧 Chrome, 确保用户能看到窗口
            # 原因: Chrome 关闭窗口后进程仍在后台保持 CDP 端口, 复用会导致看不到画面
            log("🔄 有头模式: 清理旧 Chrome 进程, 重新启动...")
            _kill_chrome_on_port(config.DEBUG_PORT)
            # 等待端口释放
            for _ in range(30):
                if not is_port_open(config.DEBUG_PORT):
                    break
                time.sleep(0.2)
        else:
            log(f"✅ 检测到 Chrome DevTools 端口 {config.DEBUG_PORT} 可用且有标签页 → 直接附加")
            return None

    if is_chrome_debug_port(config.DEBUG_PORT):
        tabs = get_chrome_tabs(config.DEBUG_PORT)
        log(f"⚠️ Chrome DevTools 端点响应但无标签页（tabs={len(tabs)}），Chrome 窗口可能已关闭")
        procs = find_port_processes(config.DEBUG_PORT)
        chrome_pids = [pid for pid, cmd in procs if "chrome" in cmd.lower()]
        if chrome_pids:
            log(f"🔧 检测到 Chrome 残留进程，尝试清理并重启...")
            kill_processes(chrome_pids)
            time.sleep(2)
            if is_port_open(config.DEBUG_PORT):
                log(f"⚠️ 清理后端口仍被占用，等待 3 秒后再检测...")
                time.sleep(3)
            if is_port_open(config.DEBUG_PORT):
                log(f"❌ 端口 {config.DEBUG_PORT} 无法释放，请手动处理")
                raise RuntimeError(f"端口 {config.DEBUG_PORT} 被占用且无法释放")
        else:
            log(f"⚠️ 未找到 Chrome 进程，端口可能处于 TIME_WAIT，等待 3 秒...")
            time.sleep(3)

    elif is_port_open(config.DEBUG_PORT):
        procs = find_port_processes(config.DEBUG_PORT)
        if procs:
            log(f"⚠️ 端口 {config.DEBUG_PORT} 被占用，但不是有效的 Chrome DevTools 端点:")
            for pid, cmd in procs:
                log(f"   PID {pid}: {cmd}")

            chrome_pids = [pid for pid, cmd in procs if "chrome" in cmd.lower()]
            if chrome_pids:
                log(f"🔧 检测到 Chrome 残留进程，尝试清理...")
                kill_processes(chrome_pids)
                time.sleep(2)
                if is_port_open(config.DEBUG_PORT):
                    log(f"⚠️ 清理后端口仍被占用，等待 3 秒后再检测...")
                    time.sleep(3)
                if is_port_open(config.DEBUG_PORT):
                    log(f"❌ 端口 {config.DEBUG_PORT} 无法释放，请手动处理")
                    raise RuntimeError(f"端口 {config.DEBUG_PORT} 被占用且无法释放")
            else:
                log(f"❌ 端口被非 Chrome 进程占用，请手动释放后重试")
                raise RuntimeError(f"端口 {config.DEBUG_PORT} 被非 Chrome 进程占用")
        else:
            log(f"⚠️ 端口 {config.DEBUG_PORT} 处于 TIME_WAIT 状态，等待 3 秒...")
            time.sleep(3)
            if is_port_open(config.DEBUG_PORT):
                log(f"❌ 端口仍被占用")
                raise RuntimeError(f"端口 {config.DEBUG_PORT} 处于不可用状态")

    log(f"🚀 启动 Chrome (port={config.DEBUG_PORT}) ...")
    cmd = [config.CHROME_PATH, f"--remote-debugging-port={config.DEBUG_PORT}",
           f"--user-data-dir={config.USER_DATA_DIR.resolve()}",
           "--no-first-run", "--no-default-browser-check"]
    if config.HEADLESS:
        cmd.append("--headless=new")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if is_port_open(config.DEBUG_PORT):
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
    if config.REUSE_EXISTING_TAB:
        for pg in context.pages:
            try:
                if config.QWEN_HOST in pg.url:
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
    for sel in config.LOGIN_DETECTORS:
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
        if await find_element(page, config.INPUT_SELECTORS, "对话输入框"):
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
