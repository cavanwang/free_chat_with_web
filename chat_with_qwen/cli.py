"""交互式 CLI 入口与命令行启动逻辑。从原 chat_with_qwen.py 拆出。"""
import argparse
import asyncio
import json
import os
import sys

from playwright.async_api import async_playwright

from . import config
from .state import log, _save_debug_screenshot
from .browser import (
    launch_chrome, find_or_create_qwen_page, wait_for_login_then_chat, find_element,
)
from .page_ops import (
    _prompt_midscene_enabled, ensure_modes, switch_model, switch_chat_mode,
    list_models, list_chat_modes,
)
from .captcha import _human_keystroke_delay_ms
from .stream import stream_chat
from .api import run_api_server
from cmd_palette import prompt_query, print_help, normalize_command


COMMANDS = {
    "/help":      "查看所有命令列表",
    "/models":    "查看可用模型列表",
    "/model":     "切换模型, 用法: /model <模型名>",
    "/chatmodes": "查看对话模式列表",
    "/chatmode":  "切换对话模式, 用法: /chatmode <模式名>",
    "/quit":      "退出程序",
}


async def main():
    log("脚本启动")
    
    # 交互式询问 Midscene(仅在非 API 模式下,且未通过命令行/环境变量确定时)
    
    # 显示运行模式
    mode_label = "有头模式(可交互)" if not config.HEADLESS else "无头模式(安全)"
    log(f"🖥️  浏览器运行模式: {mode_label}")
    log(f"   Midscene OS 级操作: {'已禁用(安全)' if not config.MIDSCENE_ENABLED else '已启用(谨慎)'}")
    if not config.MIDSCENE_ENABLED:
        log("   滑块将使用 Playwright CDP 拖拽(浏览器进程内, 不会干扰宿主机键鼠)")
    
    # 只有初始值 False 且未显式设置时才询问
    # 但命令行和环境变量的处理在 __main__ 中已完成
    # 这里再检查一次:如果 config.MIDSCENE_ENABLED 仍是初始值 False,且没有显式禁用的意图
    # 简化处理:非 API 模式下总是询问一次(用户可能想临时切换)
    _prompt_midscene_enabled()
    
    chrome_proc = launch_chrome()
    attached = (chrome_proc is None)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{config.DEBUG_PORT}")
        log("CDP 连接成功")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page, is_reused = await find_or_create_qwen_page(context)

        log("🔒 注入 stealth 环境补丁...")
        try:
            await page.add_init_script(config.STEALTH_PATCH_JS)
        except Exception:
            pass
        try:
            await page.evaluate(config.STEALTH_PATCH_JS)
            log("   ✅ 补丁已注入")
        except Exception as e:
            log(f"   ⚠️  注入部分受限: {e}")

        if not is_reused:
            await page.goto(config.QWEN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        if not await wait_for_login_then_chat(page, config.LOGIN_TIMEOUT_SEC):
            log("❌ 登录超时")
            await browser.close()
            return

        input_el = await find_element(page, config.INPUT_SELECTORS, "输入框")
        if not input_el:
            log("❌ 找不到输入框")
            await browser.close()
            return

        await ensure_modes(page, config.ENABLE_MODES)

        if config.DEFAULT_MODEL:
            await switch_model(page, config.DEFAULT_MODEL)

        if config.DEFAULT_CHAT_MODE:
            await switch_chat_mode(page, config.DEFAULT_CHAT_MODE)

        send_el = await find_element(page, config.SEND_SELECTORS, "发送按钮")

        mode = "附加" if attached else "新启动"
        tab = "复用" if is_reused else "新建"
        log(f"✅ 就绪（{mode}/{tab}）\n")
        
        # 就绪状态截图(方便无头模式下确认页面状态)
        await _save_debug_screenshot(page, "ready")

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
                ie = await find_element(page, config.INPUT_SELECTORS, "输入框")
                se = await find_element(page, config.SEND_SELECTORS, "发送按钮")
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


def run():
    parser = argparse.ArgumentParser(description="Qianwen Web Hook")
    parser.add_argument("--api", action="store_true", help="启动 OpenAI 兼容 API 服务")
    parser.add_argument("--host", default=config.API_HOST, help=f"API 监听地址（默认 {config.API_HOST}）")
    parser.add_argument("--port", type=int, default=config.API_PORT, help=f"API 监听端口（默认 {config.API_PORT}）")
    parser.add_argument("--api-key", default=config.API_KEY, help="API 鉴权密钥（留空则不鉴权）")
    parser.add_argument("--model", default=config.API_MODEL, help=f"API 模型名（默认 {config.API_MODEL}）")
    parser.add_argument("--random-sessionid", action="store_true",
                        help="每次启动生成独立随机默认会话id, 不带conversation_id的请求纯新且互不串味")
    parser.add_argument("--midscene", dest="midscene", default=None,
                        help="启用 Midscene OS 级操作 (true/false, 优先级最高)")
    parser.add_argument("--midscene-url", dest="midscene_url", default=None,
                        help=f"Midscene 服务地址 (默认 {config.MIDSCENE_BASE_URL})")
    args = parser.parse_args()

    if args.api_key:
        config.API_KEY = args.api_key
    if args.model:
        config.API_MODEL = args.model

    # ============ 确定 config.MIDSCENE_ENABLED(三种方式,优先级从高到低) ============
    # 1. 命令行参数 --midscene true/false
    if args.midscene is not None:
        if args.midscene.lower() in ('true', '1', 'yes', 'y'):
            config.MIDSCENE_ENABLED = True
        elif args.midscene.lower() in ('false', '0', 'no', 'n'):
            config.MIDSCENE_ENABLED = False
        else:
            print(f"❌ --midscene 参数无效: {args.midscene} (应为 true 或 false)")
            sys.exit(1)
        config._MIDSCENE_EXTERNAL_SET = True
        print(f"📌 Midscene (命令行): {'启用' if config.MIDSCENE_ENABLED else '未启用'}")
    # 2. 环境变量 config.MIDSCENE_ENABLED
    elif os.environ.get('MIDSCENE_ENABLED', '').lower() in ('true', '1', 'yes'):
        config.MIDSCENE_ENABLED = True
        config._MIDSCENE_EXTERNAL_SET = True
        print(f"📌 Midscene (环境变量): 启用")
    elif os.environ.get('MIDSCENE_ENABLED', '').lower() in ('false', '0', 'no'):
        config.MIDSCENE_ENABLED = False
        config._MIDSCENE_EXTERNAL_SET = True
        print(f"📌 Midscene (环境变量): 未启用")
    # 3. 交互式询问(仅非 API 模式,在 main() 中调用)

    # Midscene 服务地址(命令行优先)
    if args.midscene_url:
        config.MIDSCENE_BASE_URL = args.midscene_url
        print(f"📌 Midscene 服务地址: {config.MIDSCENE_BASE_URL}")
    elif os.environ.get('MIDSCENE_BASE_URL'):
        config.MIDSCENE_BASE_URL = os.environ['MIDSCENE_BASE_URL']

    # API 模式下,如果启用了 Midscene,提前验证服务
    if args.api and config.MIDSCENE_ENABLED:
        print()
        log("🔌 验证 Midscene 服务连通性...")
        try:
            import urllib.request
            with urllib.request.urlopen(f"{config.MIDSCENE_BASE_URL}/health", timeout=3) as resp:
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
            asyncio.run(run_api_server(args.host, args.port, args.random_sessionid))
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
