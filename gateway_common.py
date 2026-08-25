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
import time


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
