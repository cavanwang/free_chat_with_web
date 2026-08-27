import asyncio
import re
import time
from typing import AsyncGenerator

from . import config
from .state import log
from .captcha import detect_captcha, mouse_jitter


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


class CaptchaRestartNeeded(Exception):
    """(仅 API) 验证框持续未完成, 需要重启标签页并重试本轮。由 handle_chat 捕获处理。"""
    pass


def _new_wait_ctx():
    """创建单轮等待上下文, 供 _idle_decision 使用。"""
    now = time.time()
    return {
        "start_ts": now,
        "last_active": now,
        "last_text_len": 0,
        "captcha_ever": False,
        "captcha_first_at": None,
        "captcha_gone_at": None,
    }


def _idle_decision(ctx, captcha_present, cur_len, has_body, idle_timeout, allow_restart=False):
    """统一的"是否继续等待"判定, 解决验证拦截/静默失败时的无限等待。
    参数:
      ctx: _new_wait_ctx() 返回的可变字典
      captcha_present: 当前是否检测到验证框
      cur_len: 当前已渲染回复文本长度
      has_body: 是否已经产出过正文
      allow_restart: 是否允许"验证久未完成 -> 重启标签页重试"(仅 API 模式)
    返回 (action, reason):
      'continue' -> 继续等待
      'break'    -> 正常结束本轮(兜底)
      'abort'    -> 因验证未完成中止本轮(调用方应 raise)
      'restart'  -> 验证久未完成, 需重启标签页重试(仅 allow_restart 时返回)
    """
    now = time.time()

    # 硬上限: 无论如何不超过
    if now - ctx["start_ts"] > config.STREAM_OVERALL_TIMEOUT:
        return ("break", f"单轮超过 {config.STREAM_OVERALL_TIMEOUT}s 硬上限, 兜底结束")

    # 真有新文字 -> 刷新活跃时间(只有这里才刷新, 修掉旧的每轮重置 bug)
    if cur_len > ctx["last_text_len"]:
        ctx["last_text_len"] = cur_len
        ctx["last_active"] = now
        ctx["captcha_gone_at"] = None
        return ("continue", None)

    # 无新文字 + 正在验证
    if captcha_present:
        ctx["captcha_ever"] = True
        if ctx["captcha_first_at"] is None:
            ctx["captcha_first_at"] = now
        ctx["last_active"] = now
        ctx["captcha_gone_at"] = None
        elapsed = now - ctx["captcha_first_at"]
        # API 模式: 验证持续 config.CAPTCHA_RESTART_AFTER 仍未完成 -> 重启标签页重试
        # 仅在还没产出正文时重启(已出正文再重启会重复推送已流式的内容)
        if allow_restart and not has_body and elapsed > config.CAPTCHA_RESTART_AFTER:
            return ("restart", f"人机验证 {config.CAPTCHA_RESTART_AFTER}s 未完成, 重启标签页重试")
        # 交互式(不重启): 靠 config.STREAM_OVERALL_TIMEOUT 硬上限兜底, 期间等真人完成
        return ("continue", None)

    # 无新文字 + 无验证框, 但此前出现过验证(手动关闭 / 验证失败弹窗消失)
    if ctx["captcha_ever"]:
        if ctx["captcha_gone_at"] is None:
            ctx["captcha_gone_at"] = now
        if now - ctx["captcha_gone_at"] > config.CAPTCHA_GONE_GRACE:
            if not has_body:
                return ("abort", "检测到人机验证且未完成(弹窗关闭或验证失败), 本轮中止, 请手动通过后重试")
            return ("break", "验证后仍无新回复, 兜底结束")
        return ("continue", None)

    # 全程无验证: 首个 token 前给较宽上限(兼容慢启动), 出过文字后按 idle_timeout
    grace = idle_timeout if has_body else config.FIRST_TOKEN_TIMEOUT
    if now - ctx["last_active"] > grace:
        return ("break", f"{int(grace)}s 无新数据, 兜底结束")
    return ("continue", None)


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
            if config.DUMP_RAW_SSE and payload:
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

        if config.STREAM_OUTPUT:
            log("开始接收流式数据...")

        ctx = _new_wait_ctx()
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
                                # 剥离正文中的 [(multimodal_chat_think_N)] 引用标签
                                body_data = d2
                                if config.STRIP_THINK_REF_TAGS:
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
                                if config.STREAM_OUTPUT:
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
                    if config.STREAM_OUTPUT and data:
                        if not think_prefix_printed:
                            print("\033[90m💭 ", end="", flush=True)
                            think_prefix_printed = True
                        print(data, end="", flush=True)

                elif kind == "body":
                    chunk_count += 1
                    if data:
                        # 剥离正文中的 [(multimodal_chat_think_N)] 引用标签
                        body_data = data
                        if config.STRIP_THINK_REF_TAGS:
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
                captcha_present = await detect_captcha(page)
                await mouse_jitter(page)
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
                                    body_data = d2
                                    if config.STRIP_THINK_REF_TAGS:
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

                except Exception as poll_err:
                    log(f"⚠️ 轮询异常: {poll_err}")

                # 统一空闲/验证判定(含硬上限, 修掉无限等待)
                # 交互式终端里 abort 也按结束处理(用户在场可自行重试), 不抛异常
                action, reason = _idle_decision(
                    ctx, captcha_present, cur_len, bool(live_parts), idle_timeout
                )
                if action in ("abort", "break"):
                    if action == "abort":
                        log(f"🚫 {reason}")
                    else:
                        log(f"⚠️ {reason}")
                    break

        if think_prefix_printed:
            print("\033[0m")
        if stream_prefix_printed:
            print()

        final_body = "".join(live_parts)
        final_think = "".join(think_parts)

        # 剥离正文中的 [(multimodal_chat_think_N)] 引用标签
        if config.STRIP_THINK_REF_TAGS:
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

        if config.DUMP_RAW or config.DUMP_RAW_SSE:
            config.RAW_DUMP_DIR.mkdir(exist_ok=True)
            # 保存解析后内容
            if config.DUMP_RAW:
                f = config.RAW_DUMP_DIR / f"round{round_num}_stream.txt"
                content = ""
                if final_think:
                    content += f"=== 思考过程 ===\n{final_think}\n\n"
                content += f"=== 正文 ===\n{final_body}"
                f.write_text(content, encoding="utf-8")
                log(f"📄 已保存: {f.resolve()}")
            # 保存原始 SSE 行(诊断用)
            if config.DUMP_RAW_SSE and raw_sse_parts:
                f_raw = config.RAW_DUMP_DIR / f"round{round_num}_raw_sse.txt"
                f_raw.write_text("".join(raw_sse_parts), encoding="utf-8")
                log(f"📄 已保存原始SSE: {f_raw.resolve()} ({len(raw_sse_parts)}行)")
            # 保存 debug 信息
            if debug_lines:
                f_dbg = config.RAW_DUMP_DIR / f"round{round_num}_debug.txt"
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

async def stream_chat_gen(page, send_coro, round_num, idle_timeout=15, allow_restart=False) -> AsyncGenerator[tuple, None]:
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
            if config.DUMP_RAW_SSE and payload:
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
        ctx = _new_wait_ctx()
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
                                body_data = d2
                                if config.STRIP_THINK_REF_TAGS:
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
                        if config.STRIP_THINK_REF_TAGS:
                            body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                        live_parts.append(body_data)
                        yield ("body", body_data)

            except asyncio.TimeoutError:
                captcha_present = await detect_captcha(page)
                await mouse_jitter(page)
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
                                    body_data = d2
                                    if config.STRIP_THINK_REF_TAGS:
                                        body_data = re.sub(r'\[\(multimodal_chat_think_\d+\)\]', '', body_data)
                                    live_parts.append(body_data)
                                    yield ("body", body_data)
                                elif k2 == "think" and d2:
                                    think_parts.append(d2)
                                    yield ("think", d2)
                            except asyncio.QueueEmpty:
                                break
                        break

                except Exception as poll_err:
                    log(f"⚠️ 轮询异常: {poll_err}")

                # 统一空闲/验证判定(含硬上限, 修掉无限等待)
                action, reason = _idle_decision(
                    ctx, captcha_present, cur_len, bool(live_parts), idle_timeout,
                    allow_restart=allow_restart
                )
                if action == "restart":
                    log(f"🔁 {reason}")
                    raise CaptchaRestartNeeded(reason)
                elif action == "abort":
                    log(f"🚫 {reason}")
                    raise RuntimeError(reason)
                elif action == "break":
                    log(f"⚠️ {reason}")
                    break

        final_body = "".join(live_parts)
        final_think = "".join(think_parts)

        if config.STRIP_THINK_REF_TAGS:
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

        if config.DUMP_RAW or config.DUMP_RAW_SSE:
            config.RAW_DUMP_DIR.mkdir(exist_ok=True)
            if config.DUMP_RAW:
                f = config.RAW_DUMP_DIR / f"round{round_num}_stream.txt"
                content = ""
                if final_think:
                    content += f"=== 思考过程 ===\n{final_think}\n\n"
                content += f"=== 正文 ===\n{final_body}"
                f.write_text(content, encoding="utf-8")
            if config.DUMP_RAW_SSE and raw_sse_parts:
                f_raw = config.RAW_DUMP_DIR / f"round{round_num}_raw_sse.txt"
                f_raw.write_text("".join(raw_sse_parts), encoding="utf-8")
            if debug_lines:
                f_dbg = config.RAW_DUMP_DIR / f"round{round_num}_debug.txt"
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
