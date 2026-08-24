"""斜杠命令 TUI 面板，基于 prompt_toolkit。

在 `You: ` 提示符下输入 `/` 即弹出悬浮命令菜单（含命令名 + 说明），
方向键选择、回车确认、Esc 取消。若 prompt_toolkit 不可用则回退到内置 input()。
"""
from __future__ import annotations

from typing import Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML

    _HAS_PT = True
except ImportError:  # pragma: no cover - 依赖缺失时回退
    _HAS_PT = False
    PromptSession = None  # type: ignore[assignment]
    Completer = object  # type: ignore[assignment]

# 复用单个 PromptSession（保留输入历史、避免每次重建）
_session: "Optional[PromptSession]" = None


class CommandCompleter(Completer):
    """输入以 `/` 开头时弹出命令菜单；其它输入不触发，避免干扰正常聊天。"""

    def __init__(self, commands: dict):
        self._commands = commands

    def get_completions(self, document, complete_event):  # noqa: D401
        text = document.text_before_cursor
        # 仅当输入以 '/' 起始时弹出菜单，普通聊天文本不补全
        if not text.startswith("/"):
            return
        # 已含空格（参数输入阶段）则不再弹菜单，交给用户自由输入参数
        if " " in text:
            return
        for cmd, desc in self._commands.items():
            if cmd.startswith(text):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=HTML(f"<b>{cmd}</b>"),
                    display_meta=desc,
                )


def _fallback_input(prompt_str: str) -> Optional[str]:
    try:
        return input(prompt_str).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _get_session(completer) -> "PromptSession":
    """惰性创建并复用 PromptSession，每次更新 completer。"""
    global _session
    if _session is None:
        _session = PromptSession(
            completer=completer,
            complete_while_typing=True,
        )
    else:
        _session.completer = completer
    return _session


async def prompt_query(prompt_str: str = "You: ", commands: Optional[dict] = None) -> Optional[str]:
    """读取一行用户输入（异步，需在事件循环中 await）。

    有 prompt_toolkit 且提供了 commands 时使用 TUI 菜单的异步 API；
    否则回退到 input()。返回 None 表示用户 EOF / Ctrl-C 退出。
    """
    if not _HAS_PT or not commands:
        return _fallback_input(prompt_str)
    try:
        session = _get_session(CommandCompleter(commands))
        # 必须用 prompt_async: 同步 prompt() 内部会调 asyncio.run()，
        # 在已有事件循环（asyncio.run(main())）里会报 "cannot be called from a running event loop"
        text = await session.prompt_async(prompt_str)
        return text.strip()
    except (EOFError, KeyboardInterrupt):
        return None


def print_help(commands: dict, title: str = "可用命令") -> None:
    """打印命令列表 + 说明（/help 命令调用）。"""
    print(f"\n===== {title} =====")
    # 计算列宽
    max_cmd = max(len(c) for c in commands)
    for cmd, desc in commands.items():
        print(f"  {cmd:<{max_cmd}}  {desc}")
    print("=" * (len(title) + 10))
    print("提示: 输入 / 可弹出命令菜单，方向键选择，回车确认。\n")


def normalize_command(raw: str, commands: dict) -> str:
    """若 raw 以 `/` 起始且首个 token 命中注册命令，则去掉前导 '/'。

    `/models` -> `models`，`/model gpt-4` -> `model gpt-4`。
    未命中（如 `/some/unknown/path`）则原样返回，作为普通聊天文本发送。
    """
    if not raw.startswith("/"):
        return raw
    first = raw.split(" ", 1)[0].lower()
    registered = {c.lower() for c in commands}
    if first in registered:
        return raw[1:]
    return raw
