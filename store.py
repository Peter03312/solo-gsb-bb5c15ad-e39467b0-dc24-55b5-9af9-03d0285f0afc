"""SQLite 存储层：项目、版本快照与三方合并（optimistic concurrency）。

数据模型：
- projects：项目（一次成功上传对应一个项目）
- versions：每次成功合并产生一个不可变版本快照
- entries：每个版本快照下的字幕条，uid 跨版本稳定
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime

from subtitle import validate_track

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_format TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    number INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (project_id, number)
);
CREATE TABLE IF NOT EXISTS entries (
    version_id INTEGER NOT NULL REFERENCES versions(id),
    uid TEXT NOT NULL,
    seq INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_version ON entries(version_id, seq);
CREATE INDEX IF NOT EXISTS idx_versions_project ON versions(project_id, number);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_uid() -> str:
    return "e" + uuid.uuid4().hex[:11]


def _same(a, b) -> bool:
    return (
        a["start_ms"] == b["start_ms"]
        and a["end_ms"] == b["end_ms"]
        and a["text"] == b["text"]
    )


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---------- 读取 ----------

    def list_projects(self):
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT p.*, (SELECT MAX(number) FROM versions v WHERE v.project_id = p.id)
                       AS head_version
                FROM projects p ORDER BY p.id DESC
                """
            ).fetchall()

    def get_project(self, pid: int):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()

    def list_versions(self, pid: int):
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT v.number, v.note, v.created_at, COUNT(e.uid) AS entry_count
                FROM versions v LEFT JOIN entries e ON e.version_id = v.id
                WHERE v.project_id = ?
                GROUP BY v.id ORDER BY v.number DESC
                """,
                (pid,),
            ).fetchall()

    def version_info(self, pid: int, number: int):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM versions WHERE project_id = ? AND number = ?",
                (pid, number),
            ).fetchone()

    def snapshot(self, pid: int, number: int):
        """指定版本的字幕条列表（轨道顺序）；版本不存在返回 None。"""
        with self._connect() as conn:
            return self._snapshot(conn, pid, number)

    def head(self, pid: int):
        with self._connect() as conn:
            return self._head(conn, pid)

    def _snapshot(self, conn, pid: int, number: int):
        row = conn.execute(
            "SELECT id FROM versions WHERE project_id = ? AND number = ?",
            (pid, number),
        ).fetchone()
        if row is None:
            return None
        return [
            {"uid": r["uid"], "start_ms": r["start_ms"], "end_ms": r["end_ms"], "text": r["text"]}
            for r in conn.execute(
                "SELECT uid, start_ms, end_ms, text FROM entries WHERE version_id = ? ORDER BY seq",
                (row["id"],),
            )
        ]

    def _head(self, conn, pid: int):
        row = conn.execute(
            "SELECT MAX(number) AS n FROM versions WHERE project_id = ?", (pid,)
        ).fetchone()
        number = row["n"] or 0
        return number, (self._snapshot(conn, pid, number) if number else [])

    # ---------- 写入 ----------

    def create_project(self, name: str, fmt: str, entries) -> int:
        """解析成功后才调用：建项目并写入 v1 快照。"""
        with self._lock, self._connect() as conn:
            pid = conn.execute(
                "INSERT INTO projects(name, source_format, created_at) VALUES (?,?,?)",
                (name, fmt, _now()),
            ).lastrowid
            self._insert_version(conn, pid, 1, "导入原始文件", entries)
            return pid

    def _insert_version(self, conn, pid: int, number: int, note: str, entries):
        vid = conn.execute(
            "INSERT INTO versions(project_id, number, note, created_at) VALUES (?,?,?,?)",
            (pid, number, note, _now()),
        ).lastrowid
        for seq, e in enumerate(entries):
            conn.execute(
                "INSERT INTO entries(version_id, uid, seq, start_ms, end_ms, text)"
                " VALUES (?,?,?,?,?,?)",
                (vid, e.get("uid") or _new_uid(), seq, e["start_ms"], e["end_ms"], e["text"]),
            )

    # ---------- 三方合并 ----------

    def merge(self, pid: int, base_version: int, ops: list) -> dict:
        """以 base_version 为基准合并一批操作。

        返回 status：
        - merged：合并成功，产生新版本
        - noop：无有效操作
        - conflict：同一字幕条被双方改/删，返回双方状态，不落库
        - invalid：合并结果未通过整轨校验，整批拒绝
        - error：请求本身非法（如基础版本不存在、操作格式错误）
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                head_num, head_entries = self._head(conn, pid)
                if head_num == 0:
                    conn.rollback()
                    return {"status": "error", "message": "项目不存在"}
                base_entries = self._snapshot(conn, pid, base_version)
                if base_entries is None:
                    conn.rollback()
                    return {"status": "error", "message": f"基础版本 v{base_version} 不存在"}
                if not ops:
                    conn.rollback()
                    return {"status": "noop", "version": head_num}

                bmap = {e["uid"]: e for e in base_entries}
                cmap = {e["uid"]: e for e in head_entries}
                merged = [dict(e) for e in head_entries]
                midx = {e["uid"]: i for i, e in enumerate(merged)}
                conflicts, op_errors, inserts = [], [], []
                deleted_uids = set()
                n_upd = n_del = 0

                for op in ops:
                    err = self._check_op(op)
                    if err:
                        op_errors.append(err)
                        continue
                    t = op["type"]
                    if t == "update":
                        uid = op["uid"]
                        b = bmap.get(uid)
                        if b is None:
                            op_errors.append(f"更新目标 {uid} 不在基础版本 v{base_version} 中")
                            continue
                        yours = {"uid": uid, "start_ms": op["start_ms"],
                                 "end_ms": op["end_ms"], "text": op["text"]}
                        if _same(b, yours):
                            continue  # 用户未实际修改
                        c = cmap.get(uid)
                        if c is None:
                            conflicts.append({"uid": uid, "kind": "update_vs_delete",
                                              "base": b, "head": None, "yours": yours})
                        elif not _same(b, c):
                            if not _same(c, yours):  # 双方改成一致则不算冲突
                                conflicts.append({"uid": uid, "kind": "both_updated",
                                                  "base": b, "head": c, "yours": yours})
                        else:
                            merged[midx[uid]].update(yours)
                            n_upd += 1
                    elif t == "delete":
                        uid = op["uid"]
                        b = bmap.get(uid)
                        if b is None:
                            op_errors.append(f"删除目标 {uid} 不在基础版本 v{base_version} 中")
                            continue
                        c = cmap.get(uid)
                        if c is None:
                            continue  # 双方都删了
                        if not _same(b, c):
                            conflicts.append({"uid": uid, "kind": "delete_vs_update",
                                              "base": b, "head": c, "yours": None})
                        else:
                            deleted_uids.add(uid)
                            n_del += 1
                    else:  # insert
                        uid = op.get("uid") or _new_uid()
                        if uid in bmap or uid in cmap:
                            op_errors.append(f"插入的 uid {uid} 已存在")
                            continue
                        rec = {"uid": uid, "start_ms": op["start_ms"],
                               "end_ms": op["end_ms"], "text": op["text"]}
                        anchor = op.get("after_uid")
                        if anchor is not None and anchor not in cmap:
                            if anchor in bmap:
                                conflicts.append({"uid": anchor, "kind": "anchor_deleted",
                                                  "base": bmap[anchor], "head": None,
                                                  "yours": rec})
                            elif op.get("on_missing_anchor") in ("start", "end"):
                                anchor = None if op["on_missing_anchor"] == "start" else "__end__"
                                inserts.append((anchor, rec))
                            else:
                                op_errors.append(f"插入锚点 {anchor} 不存在")
                            continue
                        inserts.append((anchor, rec))

                if op_errors:
                    conn.rollback()
                    return {"status": "error", "errors": op_errors}
                if conflicts:
                    conn.rollback()
                    return {"status": "conflict", "head_version": head_num,
                            "conflicts": conflicts}

                merged = [e for e in merged if e["uid"] not in deleted_uids]
                final = [r for a, r in inserts if a is None]
                tail = [r for a, r in inserts if a == "__end__"]
                after_map = {}
                for a, r in inserts:
                    if a not in (None, "__end__"):
                        after_map.setdefault(a, []).append(r)
                for e in merged:
                    final.append(e)
                    final.extend(after_map.get(e["uid"], []))
                final.extend(tail)

                errors = validate_track(final)
                if errors:
                    conn.rollback()
                    return {"status": "invalid", "errors": errors, "head_version": head_num}

                note = (f"基于 v{base_version}：更新 {n_upd} 条，"
                        f"删除 {n_del} 条，新增 {len(inserts)} 条")
                self._insert_version(conn, pid, head_num + 1, note, final)
                conn.commit()
                return {"status": "merged", "version": head_num + 1, "note": note}
            finally:
                conn.close()

    @staticmethod
    def _check_op(op) -> str | None:
        if not isinstance(op, dict):
            return "操作必须是对象"
        t = op.get("type")
        if t not in ("update", "delete", "insert"):
            return f"未知操作类型 {t!r}"
        if t in ("update", "delete") and not isinstance(op.get("uid"), str):
            return f"{t} 操作缺少 uid"
        if t in ("update", "insert"):
            for k in ("start_ms", "end_ms"):
                v = op.get(k)
                if not isinstance(v, int) or isinstance(v, bool):
                    return f"{t} 操作的 {k} 必须为整数毫秒"
            if not isinstance(op.get("text"), str):
                return f"{t} 操作的 text 必须为字符串"
        if t == "insert":
            a = op.get("after_uid")
            if a is not None and not isinstance(a, str):
                return "insert 操作的 after_uid 必须为字符串或 null"
            if op.get("uid") is not None and not isinstance(op.get("uid"), str):
                return "insert 操作的 uid 必须为字符串"
        return None
