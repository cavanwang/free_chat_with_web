#!/usr/bin/env python3
"""千问 Web Hook —— 入口薄壳。

实际实现已拆分到 chat_with_qwen/ 包:
  config   配置常量        state    运行期状态/日志/截图
  browser  Chrome 启停/页面 captcha  滑块验证
  page_ops 模式/模型切换等  stream   流式接收
  api      OpenAI 兼容服务  cli      交互式入口 + 命令行启动

本文件仅作为 `python chat_with_qwen.py` 的入口, 转调 cli.run()。
原单体实现可从 git 找回: git show HEAD:chat_with_qwen.py
"""
from chat_with_qwen.cli import run

if __name__ == "__main__":
    run()
