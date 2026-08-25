"""
session_store.py — 跨进程共享的会话历史存储 (SQLite, WAL 模式)

设计目标:
  - 会话正本从浏览器剥离, 由网关持有。
  - qwen / deepseek 两个独立进程共读写同一个库文件, 按 conversation_id 共享同一逻辑会话,
    从而实现「同一调用方无论走 qwen 还是 deepseek 都是同一会话」。
  - 提供摘要(summary) + 近期原文(turns) 两层结构, 支撑摘要/压缩。

并发说明:
  - 采用 WAL 模式 + busy_timeout, 允许多进程/多连接并发读写。
  - 每次操作短连接(打开->操作->关闭), 调用简单、无长事务。

字段语义:
  conversations.summary         : 压缩后的旧上下文文本(可为空)
  conversations.summary_tokens  : summary 的估算 token
  turns                         : 未压缩的近期原文, 每条一行(role/content/tokens)

本模块只做存储, 不含任何 token 估算逻辑(由调用方传入 tokens)。
"""

import sqlite3
import threading
import time

# 进程内连接串行化, 避免同一进程内多协程同时写同一连接
_local_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    cid            TEXT PRIMARY KEY,
    summary        TEXT NOT NULL DEFAULT '',
    summary_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cid     TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_cid_seq ON turns(cid, seq);
"""


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL: 跨进程并发读写更友好; busy_timeout: 遇锁等待而非立即报错
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init(db_path):
    """初始化库与表结构。多次调用安全。"""
    with _local_lock:
        conn = _connect(db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


def load(db_path, cid):
    """
    读取某会话。返回:
      {
        "summary": str,
        "summary_tokens": int,
        "turns": [ {"role": str, "content": str, "tokens": int}, ... ],  # 按 seq 升序
        "total_tokens": int,   # summary_tokens + 所有 turn tokens
      }
    会话不存在时返回空结构。
    """
    with _local_lock:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT summary, summary_tokens FROM conversations WHERE cid=?", (cid,)
            ).fetchone()
            summary = row["summary"] if row else ""
            summary_tokens = row["summary_tokens"] if row else 0

            turn_rows = conn.execute(
                "SELECT role, content, tokens FROM turns WHERE cid=? ORDER BY seq ASC", (cid,)
            ).fetchall()
            turns = [
                {"role": r["role"], "content": r["content"], "tokens": r["tokens"]}
                for r in turn_rows
            ]
            total = summary_tokens + sum(t["tokens"] for t in turns)
            return {
                "summary": summary,
                "summary_tokens": summary_tokens,
                "turns": turns,
                "total_tokens": total,
            }
        finally:
            conn.close()


def _next_seq(conn, cid):
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM turns WHERE cid=?", (cid,)).fetchone()
    return (row["m"] or 0) + 1


def append_turn(db_path, cid, role, content, tokens):
    """追加一条原文(user 或 assistant)。会自动创建会话记录。"""
    with _local_lock:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO conversations(cid, summary, summary_tokens, updated_at) "
                "VALUES(?, '', 0, ?)",
                (cid, time.time()),
            )
            seq = _next_seq(conn, cid)
            conn.execute(
                "INSERT INTO turns(cid, seq, role, content, tokens) VALUES(?,?,?,?,?)",
                (cid, seq, role, content, int(tokens)),
            )
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE cid=?", (time.time(), cid)
            )
            conn.commit()
        finally:
            conn.close()


def replace_after_compaction(db_path, cid, summary, summary_tokens, kept_turns):
    """
    压缩后重写会话状态:
      - summary / summary_tokens 覆盖为新值
      - turns 全部删除, 仅重新写入 kept_turns(保留的最近若干轮)
    kept_turns: [ {"role","content","tokens"}, ... ] 按时间升序
    """
    with _local_lock:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO conversations(cid, summary, summary_tokens, updated_at) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(cid) DO UPDATE SET summary=excluded.summary, "
                "summary_tokens=excluded.summary_tokens, updated_at=excluded.updated_at",
                (cid, summary, int(summary_tokens), time.time()),
            )
            conn.execute("DELETE FROM turns WHERE cid=?", (cid,))
            for i, t in enumerate(kept_turns, start=1):
                conn.execute(
                    "INSERT INTO turns(cid, seq, role, content, tokens) VALUES(?,?,?,?,?)",
                    (cid, i, t["role"], t["content"], int(t.get("tokens", 0))),
                )
            conn.commit()
        finally:
            conn.close()


def clear(db_path, cid):
    """清空某会话(new_session=true 语义)。"""
    with _local_lock:
        conn = _connect(db_path)
        try:
            conn.execute("DELETE FROM turns WHERE cid=?", (cid,))
            conn.execute("DELETE FROM conversations WHERE cid=?", (cid,))
            conn.commit()
        finally:
            conn.close()
