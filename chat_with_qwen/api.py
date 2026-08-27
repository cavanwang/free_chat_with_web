"""OpenAI 兼容 API 服务: 上下文拼装/压缩、消息发送、路由与服务器启动。
从原 chat_with_qwen.py 拆出, 保持行为不变。"""
import asyncio
import json
import time
import uuid

from aiohttp import web
from playwright.async_api import async_playwright

import session_store
import gateway_common as gwc

from . import config
from .state import log, _app_state
from .browser import (
    launch_chrome, find_or_create_qwen_page, wait_for_login_then_chat, find_element,
)
from .page_ops import start_new_chat, reset_session, ensure_modes, switch_model
from .stream import stream_chat_gen, CaptchaRestartNeeded
from .captcha import _human_keystroke_delay_ms


def _build_chat_chunk(content=None, reasoning=None, model=None, chunk_id=None, finish=None, usage=None):
    """构建 OpenAI 兼容的 chat chunk"""
    now = int(time.time())
    msg = {
        "id": chunk_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": now,
        "model": model or config.API_MODEL,
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
        "model": model or config.API_MODEL,
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


# ============ \u4f1a\u8bdd\u4e0a\u4e0b\u6587\u62fc\u88c5 / \u538b\u7f29 ============

def _xml_escape(s):
    """\u8f6c\u4e49\u5c16\u62ec\u53f7(\u4e0e &), \u4fdd\u8bc1 <turn> \u7b49\u6807\u7b7e\u8fb9\u754c\u4e0d\u88ab\u5185\u5bb9\u91cc\u7684 '<'/'>' \u7834\u574f\u3002
    \u6362\u884c\u3001\u5f15\u53f7\u3001\u5192\u53f7\u7b49\u4e00\u5f8b\u539f\u6837\u4fdd\u7559(XML \u65e0\u9700\u8f6c\u4e49), \u4fdd\u6301\u591a\u884c\u53ef\u8bfb\u3002
    """
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_turns(turns):
    """\u628a [{role, content}, ...] \u6e32\u67d3\u6210 XML \u6807\u7b7e\u5f0f\u539f\u6587, \u6bcf\u6761\u4e00\u4e2a <wxg_turn n="\u8f6e\u6b21" role="\u89d2\u8272">\u3002
    \u8f6e\u6b21\u53f7: \u6bcf\u9047\u5230\u4e00\u6761 user \u9012\u589e(\u6211\u4eec\u6210\u5bf9\u843d\u5e93, \u4e00\u8f6e = user+assistant)\u3002
    """
    lines = []
    n = 0
    for t in turns:
        role = t.get("role", "")
        if role == "user":
            n += 1
        rn = n if n > 0 else 1
        content = _xml_escape(t.get("content", ""))
        lines.append(f'<wxg_turn n="{rn}" role="{role}">\n{content}\n</wxg_turn>')
    return "\n".join(lines)


def _assemble_context(state, user_msg):
    """\u628a[\u6b64\u524d\u6458\u8981]+[\u6700\u8fd1K\u8f6e\u539f\u6587]+[\u672c\u8f6e\u6d88\u606f]\u62fc\u6210\u4e00\u6761\u5f85\u6ce8\u5165 Web \u8f93\u5165\u6846\u7684 XML \u6807\u7b7e\u5f0f\u6587\u672c\u3002
    \u82e5\u65e2\u65e0\u6458\u8981\u4e5f\u65e0\u5386\u53f2(\u5168\u65b0\u4f1a\u8bdd\u7b2c\u4e00\u8f6e), \u76f4\u63a5\u8fd4\u56de\u539f\u59cb\u6d88\u606f\u3002
    """
    summary = (state.get("summary") or "").strip()
    turns = state.get("turns") or []
    if not summary and not turns:
        return user_msg
    parts = [config.CONTEXT_HEADER]
    if summary:
        parts.append(f"<wxg_summary>\n{_xml_escape(summary)}\n</wxg_summary>")
    if turns:
        rounds = sum(1 for t in turns if t.get("role") == "user")
        parts.append(f'<wxg_history rounds="{rounds}">\n{_render_turns(turns)}\n</wxg_history>')
    parts.append(f"<wxg_current>\n{_xml_escape(user_msg)}\n</wxg_current>")
    return "\n\n".join(parts)


async def _collect_web_reply(page, prompt_text, round_num):
    """\u5728\u5f53\u524d(\u5df2\u65b0\u5efa\u7684)\u4f1a\u8bdd\u91cc\u53d1\u9001 prompt_text \u5e76\u5b8c\u6574\u6536\u96c6\u56de\u590d\u3002
    \u8fd4\u56de (body, think)\u3002\u7528\u4e8e\u6458\u8981\u7b49\u9700\u8981\u4e00\u6b21\u6027\u62ff\u5168\u6587\u7684\u573a\u666f\u3002
    """
    async def do_send():
        await _send_message_api(page, prompt_text, fast=True)

    final_body = ""
    final_think = ""
    async for kind, data in stream_chat_gen(page, do_send(), round_num):
        if kind == "body":
            final_body += data
        elif kind == "think":
            final_think += data
        elif kind == "done":
            final_body = data.get("body", final_body)
            final_think = data.get("think", final_think)
    return final_body, final_think


async def maybe_compact(page, cid, round_num):
    """\u82e5\u4f1a\u8bdd\u7d2f\u8ba1\u4f30\u7b97 token \u8d85\u8fc7\u9608\u503c, \u89e6\u53d1\u4e00\u6b21\u538b\u7f29:
    \u53e6\u5f00\u4e00\u4e2a\u5e72\u51c0\u4f1a\u8bdd\u8ba9\u6a21\u578b\u628a[\u65e7\u6458\u8981+\u88ab\u6298\u53e0\u7684\u539f\u6587]\u538b\u6210\u65b0\u6458\u8981, \u53ea\u4fdd\u7559\u6700\u8fd1 K \u6761\u539f\u6587\u3002
    \u6458\u8981\u5931\u8d25\u5219\u9000\u5316\u4e3a"\u4fdd\u7559\u65e7\u6458\u8981 + \u6700\u8fd1 K \u6761, \u4e22\u5f03\u66f4\u65e9\u539f\u6587"\u3002
    \u8fd4\u56de\u662f\u5426\u53d1\u751f\u4e86\u538b\u7f29\u3002
    """
    state = session_store.load(config.SESSION_DB_PATH, cid)
    if state["total_tokens"] <= config.COMPACT_SOFT_LIMIT:
        return False

    turns = state["turns"]
    keep = turns[-config.COMPACT_KEEP_RECENT:] if config.COMPACT_KEEP_RECENT > 0 else []
    fold = turns[:len(turns) - len(keep)]

    conv_parts = []
    if state["summary"]:
        conv_parts.append("\u6b64\u524d\u6458\u8981:\n" + state["summary"])
    if fold:
        conv_parts.append(_render_turns(fold))
    conversation_text = "\n\n".join(conv_parts).strip()
    if not conversation_text:
        return False

    log(f"\ud83e\uddec \u89e6\u53d1\u4f1a\u8bdd\u538b\u7f29 cid={cid} (\u7d2f\u8ba1~{state['total_tokens']} tokens)")
    prompt = config.COMPACT_PROMPT_TEMPLATE.format(
        max_chars=config.COMPACT_SUMMARY_MAX_CHARS, conversation=conversation_text
    )
    try:
        page2 = await start_new_chat(page)
        _app_state["page"] = page2
        body, _think = await _collect_web_reply(page2, prompt, round_num)
        new_summary = (body or "").strip()
        if not new_summary:
            raise RuntimeError("\u6458\u8981\u4e3a\u7a7a")
        session_store.replace_after_compaction(
            config.SESSION_DB_PATH, cid, new_summary, estimate_tokens(new_summary), keep
        )
        log(f"\ud83e\uddec \u538b\u7f29\u5b8c\u6210: \u6458\u8981 {len(new_summary)} \u5b57, \u4fdd\u7559\u6700\u8fd1 {len(keep)} \u6761\u539f\u6587")
        return True
    except Exception as e:
        log(f"\u26a0\ufe0f \u538b\u7f29\u5931\u8d25({type(e).__name__}: {e}), \u9000\u5316\u4e3a\u4fdd\u7559\u6700\u8fd1 {len(keep)} \u6761")
        session_store.replace_after_compaction(
            config.SESSION_DB_PATH, cid, state["summary"], state["summary_tokens"], keep
        )
        return True


async def _send_message_api(page, query, fast=False):
    """API 模式下的消息发送。
    fast=True: 直接 fill 整段粘贴注入(用于网关拼装的长上下文, 快且稳);
    fast=False: 逐字人性化输入(反爬)。
    """
    import random
    ie = await find_element(page, config.INPUT_SELECTORS, "输入框")
    se = await find_element(page, config.SEND_SELECTORS, "发送按钮")
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

    if fast:
        # 整段粘贴注入(网关已拼好的上下文可能很长, 逐字输入既慢又易触发风控)
        try:
            await ie.fill(query)
        except Exception:
            # 退化: contenteditable 等无法 fill 时, 用 type 一次性输入
            await ie.type(query, delay=0)
        await page.wait_for_timeout(random.randint(150, 320))
    else:
        # 分段输入(人性化)
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
        if config.API_KEY:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != config.API_KEY:
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
        # 会话正本由网关持有: conversation_id 标识调用方的逻辑会话(跨 qwen/deepseek 共享)
        cid = body.get("conversation_id") or config.DEFAULT_CONVERSATION_ID
        user_msg = messages[-1].get("content", "")
        if isinstance(user_msg, list):
            user_msg = " ".join(str(c) for c in user_msg if isinstance(c, str))

        async with lock:
            page = _app_state["page"]
            if not page:
                return web.json_response({"error": {"message": "Page not ready"}}, status=503)

            # new_session=true 语义: 清空该 cid 的历史(网关侧), 从零开始
            if new_session:
                log(f"🔄 API 请求清空会话历史 cid={cid}")
                session_store.clear(config.SESSION_DB_PATH, cid)

            # 1) 取该 cid 的历史正本, 拼装[摘要+最近K轮+本轮]
            state = session_store.load(config.SESSION_DB_PATH, cid)
            injected = _assemble_context(state, user_msg)

            # 2) 点"新建对话"开一个干净的浏览器会话(与浏览器记忆解耦)
            page = await start_new_chat(page)
            if not page:
                return web.json_response({"error": {"message": "Open new chat failed"}}, status=500)
            _app_state["page"] = page

            _app_state["round_num"] += 1
            round_num = _app_state["round_num"]
            chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            log(f"===== 第 {round_num} 轮 (API) cid={cid} 注入~{estimate_tokens(injected)} tokens =====")

            # 3) 消费一轮回复; 遇验证久未完成(仅 API)则重启标签页并自动重试
            async def _consume_round():
                attempts = config.CAPTCHA_MAX_RESTARTS + 1
                for attempt in range(attempts):
                    cur_page = _app_state["page"]
                    send_coro = _send_message_api(cur_page, injected, fast=True)
                    try:
                        async for kd in stream_chat_gen(
                            cur_page, send_coro, _app_state["round_num"], allow_restart=True
                        ):
                            yield kd
                        return
                    except CaptchaRestartNeeded:
                        if attempt < attempts - 1:
                            log(f"🔁 第{attempt + 1}次验证未完成, 重启标签页后重试...")
                            newp = await reset_session(new_page=True)
                            if newp:
                                _app_state["page"] = newp
                            _app_state["round_num"] += 1
                        else:
                            raise RuntimeError(
                                "人机验证多次未完成(已重启标签页重试), 本轮中止, 请稍后重试"
                            )

            def _persist_and_maybe_usage(final_body, final_think):
                """落库(user+assistant)并返回 usage。
                本轮无有效回复(被验证拦截/静默失败)时不写历史, 避免污染后续上下文。
                """
                if final_body and final_body.strip():
                    try:
                        session_store.append_turn(
                            config.SESSION_DB_PATH, cid, "user", user_msg, estimate_tokens(user_msg)
                        )
                        session_store.append_turn(
                            config.SESSION_DB_PATH, cid, "assistant", final_body, estimate_tokens(final_body)
                        )
                    except Exception as e:
                        log(f"⚠️ 历史落库失败: {type(e).__name__}: {e}")
                else:
                    log("⚠️ 本轮无有效回复, 跳过历史落库")
                prompt_tok = estimate_tokens(injected)
                body_tok = estimate_tokens(final_body)
                think_tok = estimate_tokens(final_think)
                usage = {
                    "prompt_tokens": prompt_tok,
                    "completion_tokens": body_tok,
                    "reasoning_tokens": think_tok,
                    "total_tokens": prompt_tok + body_tok + think_tok,
                    "conversation_id": cid,
                }
                return usage

            if stream:
                async def stream_generator():
                    final_body = ""
                    final_think = ""
                    try:
                        async for kind, data in _consume_round():
                            if kind == "body":
                                final_body += data
                                yield f"data: {json.dumps(_build_chat_chunk(content=data, model=config.API_MODEL, chunk_id=chat_id), ensure_ascii=False)}\n\n"
                            elif kind == "think":
                                final_think += data
                                yield f"data: {json.dumps(_build_chat_chunk(reasoning=data, model=config.API_MODEL, chunk_id=chat_id), ensure_ascii=False)}\n\n"
                            elif kind == "done":
                                final_body = data.get("body", final_body)
                                final_think = data.get("think", final_think)
                    except Exception as e:
                        log(f"❌ API 流式异常: {type(e).__name__}: {e}")
                        yield f"data: {json.dumps(_build_chat_chunk(content=f'[error] {e}', model=config.API_MODEL, chunk_id=chat_id), ensure_ascii=False)}\n\n"

                    usage = _persist_and_maybe_usage(final_body, final_think)
                    compacted = False
                    try:
                        compacted = await maybe_compact(_app_state["page"], cid, _app_state["round_num"])
                    except Exception as e:
                        log(f"⚠️ 压缩异常: {type(e).__name__}: {e}")
                    usage["compacted"] = compacted
                    usage["session_tokens"] = session_store.load(config.SESSION_DB_PATH, cid)["total_tokens"]
                    yield f"data: {json.dumps(_build_chat_chunk(model=config.API_MODEL, chunk_id=chat_id, finish='stop', usage=usage), ensure_ascii=False)}\n\n"
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
                async for kind, data in _consume_round():
                    if kind == "body":
                        final_body += data
                    elif kind == "think":
                        final_think += data
                    elif kind == "done":
                        final_body = data.get("body", final_body)
                        final_think = data.get("think", final_think)
            except Exception as e:
                log(f"❌ API 非流式异常: {type(e).__name__}: {e}")
                return web.json_response({"error": {"message": str(e)}}, status=500)

            usage = _persist_and_maybe_usage(final_body, final_think)
            compacted = False
            try:
                compacted = await maybe_compact(_app_state["page"], cid, _app_state["round_num"])
            except Exception as e:
                log(f"⚠️ 压缩异常: {type(e).__name__}: {e}")
            usage["compacted"] = compacted
            usage["session_tokens"] = session_store.load(config.SESSION_DB_PATH, cid)["total_tokens"]
            return web.json_response(_build_chat_response(final_body, final_think, config.API_MODEL, chat_id, usage))

    async def handle_models(request):
        return web.json_response({
            "object": "list",
            "data": [{
                "id": config.API_MODEL,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "qianwen",
            }]
        })

    async def handle_reset(request):
        # API Key 鉴权
        if config.API_KEY:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != config.API_KEY:
                return web.json_response({"error": {"message": "Unauthorized"}}, status=401)

        # 可选 conversation_id: 不传则清默认会话
        cid = config.DEFAULT_CONVERSATION_ID
        try:
            rbody = await request.json()
            if isinstance(rbody, dict) and rbody.get("conversation_id"):
                cid = rbody["conversation_id"]
        except Exception:
            pass

        async with lock:
            # 网关侧: 清空该会话历史正本
            session_store.clear(config.SESSION_DB_PATH, cid)
            log(f"🔄 已清空会话历史 cid={cid}")
            return web.json_response({"status": "ok", "message": "Session cleared", "conversation_id": cid})

    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/session/reset", handle_reset)
    return app


async def run_api_server(host: str, port: int, random_sessionid: bool = False):
    """启动 API 服务器"""
    log("脚本启动（API 模式）")
    # 启动前检测端口是否已被任一 IP 占用(含 127.0.0.1 / ::1 / 通配), 占用即报错退出
    occupied, why = gwc.port_in_use(port)
    if occupied:
        log(f"❌ 端口 {port} 已被占用: {why}")
        log(f"   请换端口启动 `--port 9000` 等, 或先停掉占用进程(查: lsof -nP -iTCP:{port} -sTCP:LISTEN)。")
        return
    log(f"✅ 端口 {port} 未被占用")
    # 初始化会话历史存储(网关持有会话正本, 与 deepseek 进程共享同一 SQLite)
    try:
        session_store.init(config.SESSION_DB_PATH)
        log(f"🗄️  会话历史库: {config.SESSION_DB_PATH}")
    except Exception as e:
        log(f"⚠️ 会话历史库初始化失败: {type(e).__name__}: {e}")

    if random_sessionid:
        config.DEFAULT_CONVERSATION_ID = f"default-{uuid.uuid4().hex[:12]}"
        log(f"🆕 本次启动随机默认会话id: {config.DEFAULT_CONVERSATION_ID}")
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
            log(f"   ⚠️ 注入部分受限: {e}")

        if not is_reused:
            await page.goto(config.QWEN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        if not await wait_for_login_then_chat(page, config.LOGIN_TIMEOUT_SEC):
            log("❌ 登录超时")
            await browser.close()
            return

        await ensure_modes(page, config.ENABLE_MODES)
        if config.DEFAULT_MODEL:
            await switch_model(page, config.DEFAULT_MODEL)

        send_el = await find_element(page, config.SEND_SELECTORS, "发送按钮")

        _app_state["page"] = page
        _app_state["browser"] = browser
        _app_state["context"] = context
        _app_state["lock"] = asyncio.Lock()

        mode = "附加" if attached else "新启动"
        tab = "复用" if is_reused else "新建"
        log(f"✅ 就绪（{mode}/{tab}）")
        log(f"🚀 API 服务启动: http://{host}:{port}")
        log(f"   POST /v1/chat/completions  - 聊天(每次新建会话+注入历史)")
        log(f"   POST /v1/session/reset    - 清空会话历史(可带 conversation_id)")
        log(f"   GET  /v1/models           - 模型列表")
        if config.API_KEY:
            log(f"   API Key: {config.API_KEY}")
        log(f"   参数 conversation_id 区分逻辑会话; new_session=true 清空该会话历史")
        log(f"   会话正本存于 {config.SESSION_DB_PATH} (与 deepseek 共享)")
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
