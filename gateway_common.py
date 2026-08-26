"""
gateway_common.py — qwen / deepseek 网关封装共用的纯逻辑

放这里的都是**不依赖具体浏览器/页面**的纯函数, 供两个进程复用, 保证行为一致:
  - 上下文拼装(XML 标签式) + 尖括号转义
  - 单轮等待判定(修掉验证/静默失败时的无限等待, 带硬上限)

会话历史的存取见 session_store.py。
注: 当前 chat_with_qwen.py 仍保留了自己的一份等价实现(未迁移, 避免动到已跑通的版本);
    chat_deepseek_web.py 直接复用本模块。两边输出格式保持一致。
"""

import socket
import os
import time
import json
import re


# ============ 端口占用检测 ============

def port_in_use(port):
    """检测端口是否已被**任一 IP** 占用(含 127.0.0.1 / ::1 / 通配 0.0.0.0 / ::)。
    返回 (占用?bool, 原因str)。
    策略:
      1) 主动连回环 v4/v6: 连通即已有服务在监听(通配监听也能从回环连上, 能抓到绑在
         127.0.0.1 的应用, 如 macOS 上抢 8000 的 Beem H);
      2) 再用**不设 SO_REUSEADDR** 的绑定探测(0.0.0.0 与 127.0.0.1): 具体地址冲突也能抓到。
    """
    # 1) 连回环探测
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        s.settimeout(0.5)
        try:
            if s.connect_ex((addr, port)) == 0:
                return True, f"{addr}:{port} 已有服务在监听"
        except OSError:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    # 2) 绑定探测(不设 SO_REUSEADDR)
    for addr in ("0.0.0.0", "127.0.0.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            s.bind((addr, port))
        except OSError as e:
            return True, f"{addr}:{port} 无法绑定({e})"
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False, ""


# ============ 上下文拼装(XML 标签式) ============

def xml_escape(s):
    """转义 & 和尖括号, 保证 <turn> 等标签边界不被内容里的 '<'/'>' 破坏。
    换行/引号/冒号等原样保留(XML 无需转义), 保持多行可读。
    """
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_turns(turns):
    """把 [{role, content}, ...] 渲染成 XML 标签式原文, 每条一个 <wxg_turn n="轮次" role="角色">。
    轮次号: 每遇到一条 user 递增(成对落库时, 一轮 = user+assistant)。
    """
    lines = []
    n = 0
    for t in turns:
        role = t.get("role", "")
        if role == "user":
            n += 1
        rn = n if n > 0 else 1
        content = xml_escape(t.get("content", ""))
        lines.append(f'<wxg_turn n="{rn}" role="{role}">\n{content}\n</wxg_turn>')
    return "\n".join(lines)


def assemble_context(state, user_msg, header):
    """把[此前摘要]+[最近K轮原文]+[本轮消息]拼成一条待注入 Web 输入框的 XML 文本。
    若既无摘要也无历史(全新会话第一轮), 直接返回原始消息。
    header: 说明各标签含义的开头提示文本。
    """
    summary = (state.get("summary") or "").strip()
    turns = state.get("turns") or []
    if not summary and not turns:
        return user_msg
    parts = [header]
    if summary:
        parts.append(f"<wxg_summary>\n{xml_escape(summary)}\n</wxg_summary>")
    if turns:
        rounds = sum(1 for t in turns if t.get("role") == "user")
        parts.append(f'<wxg_history rounds="{rounds}">\n{render_turns(turns)}\n</wxg_history>')
    parts.append(f"<wxg_current>\n{xml_escape(user_msg)}\n</wxg_current>")
    return "\n\n".join(parts)


# ============ 工具调用(function calling)文本化桥接 ============
# 网页聊天本身没有 function-calling 协议, 这里用"哨兵起止符 + 每请求 nonce"把工具调用
# 模拟成纯文本: 请求侧注入 tools 定义 + 协议说明; 响应侧把模型吐出的哨兵块解析回
# OpenAI tool_calls。仅当调用方请求带 tools 时启用(agentic 客户端如 Trae)。

def _tool_markers(nonce):
    start = f"⟦⟦⟦TOOLCALL:{nonce}⟧⟧⟧"
    end = f"⟦⟦⟦/TOOLCALL:{nonce}⟧⟧⟧"
    return start, end


def _content_to_text(content):
    """把 OpenAI 消息 content(字符串 / content-parts 列表 / None)归一化为字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def tool_protocol_instruction(nonce):
    """告诉模型:需要调用工具时,只输出本轮 nonce 哨兵包裹的一段 JSON。"""
    start, end = _tool_markers(nonce)
    return (
        "你可以调用下列工具来完成任务(function calling)。\n"
        "当你需要调用某个工具时, 必须【只】输出如下起止符包裹的一段 JSON, 起止符之外不要写任何文字:\n"
        f"{start}\n"
        '{"name": "工具名", "arguments": {参数对象}}\n'
        f"{end}\n"
        "要求: 1) 起止符必须原样成对出现, 不要改动其中的编号; "
        "2) 中间只放一个合法 JSON 对象, name 为工具名, arguments 为参数对象(无参数则为 {}); "
        "3) 一次只调用一个工具; "
        "4) 若无需调用工具, 就正常用自然语言回答, 不要输出起止符。\n"
        "读取纪律(重要, 避免上下文超长): "
        "a) 读大文件时优先用 Read 的 offset/limit 分段读, 不要一次读整文件; "
        "b) 能先用 Grep/Glob 定位再按需读相关片段, 别把整文件全塞进来; "
        "c) 一次只读一个文件, 不要在一轮里并行读一大批文件。"
    )


def render_tools_as_text(tools):
    """把 OpenAI tools(function schema 列表)渲染成文本块。无工具返回空串。"""
    if not tools:
        return ""
    lines = ["<available_tools>"]
    for t in tools:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        lines.append(f"- name: {name}")
        if desc:
            lines.append(f"  description: {desc}")
        try:
            params_str = json.dumps(params, ensure_ascii=False)
        except Exception:
            params_str = str(params)
        lines.append(f"  parameters(JSON Schema): {params_str}")
    lines.append("</available_tools>")
    return "\n".join(lines)


TOOL_CONTEXT_HEADER = (
    "下面是一次带工具调用的任务。<available_tools> 列出你可用的工具(含名称、说明、参数 JSON Schema); "
    "<wxg_history> 内按顺序给出对话原文: <sys> 是系统指令, <user> 是用户消息, "
    "<assistant> 是你之前的回答, <tool_result> 是工具执行结果(name 标明它是哪个工具的结果); "
    "你之前发起的工具调用以\"(历史操作: ...)\"一行给出。请在此背景上继续完成任务, 不要复述背景本身。"
)


# 整块删除的脚手架标签(Trae 注入的环境/提醒类元信息, 对网页模型是噪音且诱导"续写模板"复读)
_SCAFFOLD_BLOCK_TAGS = (
    "system-reminder", "system_info", "environment_details",
    "important-instruction-reminders", "command-message", "command-name",
)
# 只解包、保留内部文本的标签(真实任务/内容在其中)
_SCAFFOLD_UNWRAP_TAGS = ("user_input", "user_query", "task")
_SCAFFOLD_BLOCK_RE = re.compile(
    r"<(" + "|".join(_SCAFFOLD_BLOCK_TAGS) + r")\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_SCAFFOLD_ORPHAN_RE = re.compile(
    r"</?(" + "|".join(_SCAFFOLD_BLOCK_TAGS) + r")\b[^>]*>", re.IGNORECASE
)
_SCAFFOLD_UNWRAP_RE = re.compile(
    r"</?(" + "|".join(_SCAFFOLD_UNWRAP_TAGS) + r")\b[^>]*>", re.IGNORECASE
)

def _strip_scaffolding(text):
    """剥掉 Trae 内部脚手架标记: 环境/提醒类整块删除, user_input 等只解包保留任务文本。
    这些对网页模型是噪音, 且会诱导模型"续写模板"(把脚手架原样复读而不调用工具)。"""
    if not text:
        return text
    text = _SCAFFOLD_BLOCK_RE.sub("", text)   # 成对块整体删
    text = _SCAFFOLD_ORPHAN_RE.sub("", text)  # 残留孤立标签删
    text = _SCAFFOLD_UNWRAP_RE.sub("", text)  # 解包保留内部文本
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_messages_for_tools(messages, tools, nonce, header=None):
    """工具模式:直接用调用方发来的 messages(含 system / tool_calls / tool 结果)
    渲染成一段待注入网页的文本。不走 SQLite 历史。"""
    parts = []
    parts.append(header or TOOL_CONTEXT_HEADER)
    # 注入项目根目录: Trae 的工具 cwd 可能是 home 而非项目, 且请求里往往不含项目路径,
    # 导致模型对 "." / 相对路径调工具时跑到错误目录。这里显式给出项目绝对路径并要求用绝对路径。
    _proj = os.environ.get("PROJECT_ROOT") or os.getcwd()
    parts.append(
        f"【项目根目录(绝对路径)】: {_proj}\n"
        "本次任务默认针对该项目根目录。调用文件/目录类工具(如 LS/Glob/Grep/Read/RunCommand 等)时, "
        "【必须】使用该项目根目录或其下的【绝对路径】; 【不要】使用 '.'、相对路径或省略 path, "
        "因为工具执行时的当前目录可能不是本项目目录(可能是用户主目录), 用相对路径会定位到错误位置。"
    )
    parts.append(tool_protocol_instruction(nonce))
    tools_text = render_tools_as_text(tools)
    if tools_text:
        parts.append(tools_text)
    # 预扫: tool_call_id -> 工具名。因为 Trae 的 tool 结果消息不带工具名(name 为空),
    # 用这个映射给 <tool_result> 补上"是哪个工具的结果", 让结果自带标签。
    id2name = {}
    for _m in (messages or []):
        if isinstance(_m, dict) and _m.get("role") == "assistant":
            for _tc in (_m.get("tool_calls") or []):
                if isinstance(_tc, dict):
                    _id = _tc.get("id")
                    _nm = (_tc.get("function") or {}).get("name")
                    if _id and _nm:
                        id2name[_id] = _nm
    parts.append("<wxg_history>")
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        if role == "system":
            _c = _strip_scaffolding(_content_to_text(m.get("content")))
            if _c:
                parts.append(f"<sys>\n{xml_escape(_c)}\n</sys>")
        elif role == "user":
            _c = _strip_scaffolding(_content_to_text(m.get("content")))
            if _c:
                parts.append(f"<user>\n{xml_escape(_c)}\n</user>")
        elif role == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                nm = fn.get("name", "")
                args = fn.get("arguments", "")
                # 中性一行文案: 只作"历史操作标签", 不用 XML 调用语法, 避免在历史里
                # 示范一个与"起止符 JSON"要求相竞争的格式, 加剧格式漂移。
                parts.append(
                    f"(历史操作: 你之前调用了工具 {xml_escape(str(nm))}, "
                    f"参数: {xml_escape(str(args))})"
                )
            c = _content_to_text(m.get("content"))
            if c:
                parts.append(f"<assistant>\n{xml_escape(c)}\n</assistant>")
        elif role == "tool":
            tcid = m.get("tool_call_id", "")
            nm = m.get("name", "") or id2name.get(tcid, "")
            parts.append(
                f'<tool_result name="{xml_escape(str(nm))}" id="{xml_escape(str(tcid))}">\n'
                f"{xml_escape(_content_to_text(m.get('content')))}\n</tool_result>"
            )
    parts.append("</wxg_history>")
    start, end = _tool_markers(nonce)
    parts.append(
        "现在请输出你的下一步。若需要调用工具, 【必须且只能】用下面这一种格式: 起止符包裹的一段 JSON。\n"
        "起止符之外不要写任何字; 【严禁】使用 XML 标签(如 <toolcall>/<invoke>/<parameter>)、"
        "```json 代码块、或 Action:/Action Input: 等任何其它格式。\n"
        "格式定义:\n"
        f"{start}\n"
        '{"name": "工具名", "arguments": {参数对象}}\n'
        f"{end}\n"
        "正确示例(工具名与参数都放在同一个 JSON 里, 参数是 arguments 的键值对, 不要另起标签):\n"
        f"{start}\n"
        '{"name": "RunCommand", "arguments": {"command": "pwd", "blocking": true, "requires_approval": false}}\n'
        f"{end}\n"
        "一次只调用一个工具; 若无需工具, 直接用自然语言给出最终回答。"
    )
    return "\n\n".join(parts)


def fit_messages_for_tools(messages, tools, nonce, budget_chars, per_result_cap=75000):
    """把工具模式注入裁剪到 budget_chars 字符以内, 尽量保活 agent 循环。
    返回 (injected 或 None, note):
      injected=None -> 裁剪后仍超预算, 交由上层返回 context_length_exceeded。
    顺序: 原样 -> 截断超大单条 tool 结果 -> 从最老起丢弃 tool 结果 -> 仍超则 None。
    """
    injected = render_messages_for_tools(messages, tools, nonce)
    if len(injected) <= budget_chars:
        return injected, "fit"

    msgs = [dict(m) for m in (messages or [])]

    # 1) 截断超大的单条 tool 结果(含最新的; 过长文件只保留前 per_result_cap 字符)
    truncated = 0
    for m in msgs:
        if m.get("role") == "tool":
            c = _content_to_text(m.get("content"))
            if len(c) > per_result_cap:
                m["content"] = c[:per_result_cap] + "\n…[工具结果过长, 已截断]"
                truncated += 1
    injected = render_messages_for_tools(msgs, tools, nonce)

    # 2) 从最老起逐条丢弃 tool 结果(文件内容是大头; assistant 的调用记录很小, 保留)
    dropped = 0
    while len(injected) > budget_chars:
        idx = next((i for i, m in enumerate(msgs) if m.get("role") == "tool"), None)
        if idx is None:
            break
        msgs.pop(idx)
        dropped += 1
        injected = render_messages_for_tools(msgs, tools, nonce)

    if len(injected) > budget_chars:
        return None, f"over-budget:{len(injected)}(truncated={truncated},dropped={dropped})"

    if truncated or dropped:
        notice = f"(注: 为适应长度限制, 已截断 {truncated} 条超大工具结果、省略最早 {dropped} 条工具结果。)\n\n"
        injected = notice + injected
        return injected, f"trimmed(truncated={truncated},dropped={dropped})"
    return injected, "fit"


def parse_tool_call(text, nonce):
    """从模型输出中解析哨兵包裹的工具调用。
    返回:
      None                            -> 无工具调用(普通文本回答)
      {"name":str, "arguments":dict}  -> 解析成功
      ("error", 原因str)              -> 命中起止符但内容非法(供上层决定重试/降级)
    """
    if not text:
        return None
    start, end = _tool_markers(nonce)
    si = text.find(start)
    if si < 0:
        return None
    ei = text.find(end, si + len(start))
    if ei < 0:
        return ("error", "缺少结束哨兵(可能被截断)")
    payload = text[si + len(start):ei].strip()
    if payload.startswith("```"):
        payload = payload.strip("`").strip()
        nl = payload.find("\n")
        if nl >= 0 and payload[:nl].strip().lower() in ("json", ""):
            payload = payload[nl + 1:].strip()
    try:
        obj = json.loads(payload)
    except Exception as e:
        return ("error", f"JSON 解析失败: {e}")
    if not isinstance(obj, dict) or "name" not in obj:
        return ("error", "缺少 name 字段")
    args = obj.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            pass
    return {"name": obj.get("name"), "arguments": args if isinstance(args, dict) else {}}


# ============ 单轮等待判定(超时/验证感知, 防无限等待) ============

def new_wait_ctx():
    """创建单轮等待上下文, 供 idle_decision 使用。"""
    now = time.time()
    return {
        "start_ts": now,
        "last_active": now,
        "last_text_len": 0,
        "captcha_ever": False,
        "captcha_first_at": None,
        "captcha_gone_at": None,
    }


def idle_decision(ctx, captcha_present, cur_len, has_body, idle_timeout,
                  overall_timeout, first_token_timeout,
                  captcha_gone_grace=8, captcha_restart_after=60, allow_restart=False):
    """统一的"是否继续等待"判定, 解决验证拦截/静默失败时的无限等待。
    返回 (action, reason):
      'continue' -> 继续等待
      'break'    -> 正常结束本轮(兜底)
      'abort'    -> 因验证未完成中止本轮(调用方应 raise)
      'restart'  -> 验证久未完成需重启标签页重试(仅 allow_restart 时返回)
    DeepSeek 无验证码: captcha_present 恒 False、allow_restart=False,
    则只用到 硬上限 / 首token超时 / idle 超时 三条, 天然修掉死等。
    """
    now = time.time()

    # 硬上限: 无论如何不超过
    if now - ctx["start_ts"] > overall_timeout:
        return ("break", f"单轮超过 {overall_timeout}s 硬上限, 兜底结束")

    # 真有新文字 -> 刷新活跃时间(只有这里才刷新, 修掉每轮重置 bug)
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
        if allow_restart and not has_body and elapsed > captcha_restart_after:
            return ("restart", f"人机验证 {captcha_restart_after}s 未完成, 重启标签页重试")
        return ("continue", None)

    # 无新文字 + 无验证框, 但此前出现过验证(手动关闭 / 验证失败弹窗消失)
    if ctx["captcha_ever"]:
        if ctx["captcha_gone_at"] is None:
            ctx["captcha_gone_at"] = now
        if now - ctx["captcha_gone_at"] > captcha_gone_grace:
            if not has_body:
                return ("abort", "检测到人机验证且未完成(弹窗关闭或验证失败), 本轮中止, 请手动通过后重试")
            return ("break", "验证后仍无新回复, 兜底结束")
        return ("continue", None)

    # 全程无验证: 首个 token 前给较宽上限, 出过文字后按 idle_timeout
    grace = idle_timeout if has_body else first_token_timeout
    if now - ctx["last_active"] > grace:
        return ("break", f"{int(grace)}s 无新数据, 兜底结束")
    return ("continue", None)


def build_normalizer_prompt(raw_output, tool_names):
    """构造"工具调用归一化"提示: 无历史, 只把一段可能含工具调用的原始输出转成严格 JSON。
    专治网页模型 tool call 格式不稳——任务范围极窄, 遵从率远高于原始 agentic 调用。"""
    names = ", ".join([n for n in (tool_names or []) if n]) or "(未提供)"
    return (
        "你现在是一个严格的【格式转换器】, 不要执行任何任务、不要推理、不要补充或想象内容。\n"
        "下面 <<< >>> 之间是某模型的原始输出, 其中可能包含一次或多次工具调用, 但格式不规范。\n"
        "请仅提取其中真实出现的工具调用, 转换为严格 JSON 后输出。\n"
        f"可用工具名(name 只能取其一): {names}\n"
        "输出要求: 只输出 JSON 本身, 不要解释、不要代码块围栏、不要任何 XML 标签。\n"
        '单个调用输出: {"name": "工具名", "arguments": {键值对}}\n'
        '多个调用输出 JSON 数组: [{"name": "...", "arguments": {...}}, ...]\n'
        "若原始输出里并没有任何工具调用, 则只输出四个大写字母: NONE\n"
        "<<<\n"
        + (raw_output or "")
        + "\n>>>"
    )


def parse_tool_calls(text, nonce, tool_names=None):
    """从模型输出解析工具调用, 兼容多种格式与并行多调用。
    返回 (calls, note):
      calls: list[{"name":str,"arguments":dict}], 空列表=无工具调用(普通文本)。
      note:  命中来源 / 失败原因, 供日志观测遵从率。
    """
    if not text:
        return [], "empty"

    def _norm(obj):
        if not isinstance(obj, dict):
            return None
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        if isinstance(name, dict):
            name = name.get("name")
        if not name:
            return None
        args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters") or obj.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        return {"name": name, "arguments": args if isinstance(args, dict) else {}}

    def _collect(payload):
        out = []
        try:
            data = json.loads(payload)
        except Exception:
            return out
        items = data if isinstance(data, list) else [data]
        for it in items:
            n = _norm(it)
            if n:
                out.append(n)
        return out

    def _scan_json_objects(t):
        """扫描文本里所有平衡花括号的 JSON 对象片段(应对被非法标签包裹的裸 JSON)。"""
        out = []
        i, n = 0, len(t)
        while i < n:
            if t[i] == "{":
                depth = 0
                in_str = False
                esc = False
                j = i
                while j < n:
                    c = t[j]
                    if in_str:
                        if esc:
                            esc = False
                        elif c == "\\":
                            esc = True
                        elif c == '"':
                            in_str = False
                    else:
                        if c == '"':
                            in_str = True
                        elif c == "{":
                            depth += 1
                        elif c == "}":
                            depth -= 1
                            if depth == 0:
                                out.append(t[i:j + 1])
                                break
                    j += 1
                i = j + 1
            else:
                i += 1
        return out

    # 1) 哨兵包裹(主格式)
    start, end = _tool_markers(nonce)
    si = text.find(start)
    if si >= 0:
        ei = text.find(end, si + len(start))
        payload = text[si + len(start): ei] if ei >= 0 else text[si + len(start):]
        payload = payload.strip()
        if payload.startswith("```"):
            payload = payload.strip("`").strip()
            nl = payload.find("\n")
            if nl >= 0 and payload[:nl].strip().lower() in ("json", ""):
                payload = payload[nl + 1:].strip()
        calls = _collect(payload)
        if calls:
            return calls, ("sentinel" if ei >= 0 else "sentinel-no-end")

    # 2) 兜底: XML 式工具调用。模型常吐各种方言, 这里标签名/属性都做宽容:
    #    标签: tool_call / toolcall / tool-call / invoke / function_call
    #    属性: name 可与其它属性(style=, string="true"...)共存, 顺序任意
    #    参数: <parameter name="k" ...>v</parameter> 或同标签 <toolcall name="k">v</toolcall>
    #    节点内容若是 JSON 直接解析。用工具名清单消歧。
    names_set = set(tool_names) if tool_names else None

    def _coerce(v):
        v = (v or "").strip()
        try:
            return json.loads(v)
        except Exception:
            return v

    def _attr(attrs, key):
        m = re.search(key + r'\s*=\s*"([^"]*)"', attrs or "", re.I)
        return m.group(1) if m else None

    _CALL_TAG = r"(?:tool[_-]?call|invoke|function[_-]?call)"

    # 2a) 调用标签节点内容是 JSON 工具格式 -> 直接解析(覆盖 <toolcall style=...>{JSON}</toolcall>)
    node_re = re.compile(r"<" + _CALL_TAG + r"\b[^>]*>(.*?)</" + _CALL_TAG + r">", re.S | re.I)
    node_calls = []
    for mm in node_re.finditer(text):
        inner = mm.group(1).strip()
        if inner.startswith("```"):
            inner = inner.strip("`").strip()
            nl = inner.find("\n")
            if nl >= 0 and inner[:nl].strip().lower() in ("json", ""):
                inner = inner[nl + 1:].strip()
        node_calls.extend(_collect(inner))
    if node_calls:
        return node_calls, "xml-json"

    # 2b) name= 属性式(标准 invoke/parameter、同标签方言、tool_call 下划线、参数带额外属性都覆盖)
    token = re.compile(
        r"<" + _CALL_TAG + r"\b([^>]*)>([^<]*)"
        r"|<parameter\b([^>]*)>(.*?)</parameter>",
        re.S | re.I,
    )
    xml_calls = []
    cur = None
    for mm in token.finditer(text):
        if mm.group(1) is not None:
            nm = _attr(mm.group(1), "name")
            inline = mm.group(2)
            if nm and names_set is not None and nm in names_set:
                cur = {"name": nm, "arguments": {}}
                xml_calls.append(cur)
            elif nm and names_set is None:
                cur = {"name": nm, "arguments": {}}
                xml_calls.append(cur)
            elif nm:
                if cur is None:
                    cur = {"name": nm, "arguments": {}}
                    xml_calls.append(cur)
                else:
                    cur["arguments"][nm] = _coerce(inline)
        else:
            key = _attr(mm.group(3), "name")
            if key and cur is not None:
                cur["arguments"][key] = _coerce(mm.group(4))
    if xml_calls:
        return xml_calls, "xml-toolcall"

    # 3) 兜底: ReAct  Action: X / Action Input: {json}
    react = re.findall(r"Action\s*:\s*([A-Za-z0-9_\-]+)\s*Action\s*Input\s*:\s*(\{.*?\})", text, re.S)
    if react:
        calls = []
        for name, argstr in react:
            try:
                args = json.loads(argstr)
            except Exception:
                args = {}
            calls.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
        if calls:
            return calls, "react"

    # 4) 兜底: ```json 代码块内含 {name,arguments}(或其数组)
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, re.S):
        calls = _collect(block.strip())
        if calls:
            return calls, "fenced-json"

    # 5) 兜底: 文本任意位置的裸 JSON 对象(可能被 <toolcall style=...> 等非法标签包裹)。
    #    用工具清单过滤, 只采纳 name 命中已知工具的, 避免误命中普通 JSON 文本。
    names_set2 = set(tool_names) if tool_names else None
    for blob in _scan_json_objects(text):
        cand = _collect(blob)
        if names_set2 is not None:
            cand = [c for c in cand if c.get("name") in names_set2]
        if cand:
            return cand, "bare-json"

    return [], "no-toolcall"
