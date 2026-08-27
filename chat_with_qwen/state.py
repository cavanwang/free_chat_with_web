# -*- coding: utf-8 -*-
"""共享全局状态与日志。"""
import time

from . import config

# ============ 全局状态 ============
_app_state = {
    "page": None,
    "browser": None,
    "context": None,
    "lock": None,
    "round_num": 0,
}

# 全局 run.log 文件句柄 (所有日志同时写入此文件)
RUN_LOG_PATH = config._PROJECT_ROOT / "run.log"
_run_log_file = open(RUN_LOG_PATH, "a", encoding="utf-8")


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S") + f",{int(time.time() * 1000) % 1000:03d}"
    line = f"[执行日志] {ts} {msg}"
    print(line, flush=True)
    _run_log_file.write(line + "\n")
    _run_log_file.flush()


# run.log 启动标记
_run_log_file.write(f"\n{'=' * 60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] === Python Script Started ===\n")
_run_log_file.flush()


async def _save_debug_screenshot(page, label: str, round_num: int = 0):
    """在关键节点自动截图存档,方便无头模式下排查问题"""
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"r{round_num:03d}_{label}_{ts}.png"
        filepath = config.DEBUG_SCREENSHOT_DIR / filename
        await page.screenshot(path=str(filepath), full_page=False)
        log(f"   📸 [调试截图] {filepath}")
    except Exception as e:
        log(f"   ⚠️  调试截图失败: {e}")
