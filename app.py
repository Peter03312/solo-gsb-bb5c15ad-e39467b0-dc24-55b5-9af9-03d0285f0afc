#!/usr/bin/env python3
"""自托管字幕协校服务。

- 上传 UTF-8 SRT/VTT，解析为带稳定 uid 的字幕条
- 每次提交携带编辑时的基线版本；三方合并，冲突返回双方状态，绝不覆盖
- 每次合并后整轨校验，不合规则整批拒绝
- 历史版本可查看、可按版本导出 SRT/VTT
"""
import json
import os
import re
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import subtitles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "app.db"))
PORT = int(os.environ.get("PORT", "8000"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  format TEXT NOT NULL CHECK (format IN ('srt', 'vtt')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (project_id, seq)
);
CREATE TABLE IF NOT EXISTS cues (
  version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
  uid TEXT NOT NULL,
  ord INTEGER NOT NULL,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (version_id, uid)
);
"""

# 串行化「读头版本-合并-写新版本」全过程，避免并发提交互相覆盖
_write_lock = threading.Lock()


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.commit()
    conn.close()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_cues(conn, version_id):
    rows = conn.execute(
        "SELECT uid, start_ms, end_ms, text FROM cues WHERE version_id = ? ORDER BY ord",
        (version_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_head(conn, project_id):
    return conn.execute(
        "SELECT * FROM versions WHERE project_id = ? ORDER BY seq DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def insert_version(conn, project_id, note, cues):
    """cues 中 uid 为空的条目视为新增字幕条，分配新的稳定 uid。"""
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM versions WHERE project_id = ?",
        (project_id,),
    ).fetchone()["seq"]
    vid = conn.execute(
        "INSERT INTO versions (project_id, seq, note) VALUES (?, ?, ?)",
        (project_id, seq, note or ""),
    ).lastrowid
    for i, c in enumerate(cues):
        conn.execute(
            "INSERT INTO cues (version_id, uid, ord, start_ms, end_ms, text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (vid, c.get("uid") or uuid.uuid4().hex, i,
             int(c["start_ms"]), int(c["end_ms"]), c["text"]),
        )
    return vid, seq


def _cue_eq(a, b):
    """字幕条相等性；None 表示该方已删除此条。"""
    if a is None or b is None:
        return a is b
    return (a["start_ms"] == b["start_ms"]
            and a["end_ms"] == b["end_ms"]
            and a["text"] == b["text"])


def three_way_merge(base, head, ours):
    """以 base 为共同祖先，合并当前头版本 head 与本次提交 ours。

    返回 (merged, conflicts)。有冲突时 merged 为 None。
    冲突条目：{"uid", "base", "ours", "theirs"}，ours/theirs 为 None 表示该方已删除。
    """
    base_m = {c["uid"]: c for c in base}
    head_m = {c["uid"]: c for c in head}
    ours_m = {c["uid"]: c for c in ours if c.get("uid")}
    added = [dict(c) for c in ours if not c.get("uid")]

    merged, conflicts = [], []
    for uid, b in base_m.items():
        h = head_m.get(uid)
        o = ours_m.get(uid)
        changed_ours = not _cue_eq(b, o)
        changed_head = not _cue_eq(b, h)
        if not changed_ours:
            if h is not None:
                merged.append(dict(h))
        elif not changed_head:
            if o is not None:
                merged.append(dict(o))
        elif _cue_eq(o, h):  # 双方改成了一样（含都删除）
            if o is not None:
                merged.append(dict(o))
        else:
            conflicts.append({"uid": uid, "base": b, "ours": o, "theirs": h})
    if conflicts:
        return None, conflicts
    merged.extend(added)
    merged.sort(key=lambda c: (c["start_ms"], c["end_ms"]))
    return merged, []


# ---------------------------------------------------------------- 业务操作

def create_project(qs, body):
    name = (qs.get("name") or [""])[0].strip()
    filename = (qs.get("filename") or [""])[0].strip()
    if not name:
        name = os.path.splitext(os.path.basename(filename))[0] or "未命名项目"
    if not body:
        return 400, {"error": "文件内容为空，已拒收，未创建项目"}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return 400, {"error": "文件不是有效的 UTF-8 编码，已拒收，未创建项目"}
    try:
        fmt, cues = subtitles.parse(text, filename)
    except subtitles.ParseError as e:
        return 400, {"error": f"文件格式错误，已拒收，未创建项目：{e}"}
    errors = subtitles.validate(cues)
    if errors:
        return 400, {"error": "文件内容不符合字幕轨规则，已拒收，未创建项目",
                     "reasons": errors}
    with _write_lock:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pid = conn.execute(
                "INSERT INTO projects (name, format) VALUES (?, ?)",
                (name, fmt),
            ).lastrowid
            vid, seq = insert_version(conn, pid, "导入原始文件", cues)
            conn.commit()
            return 200, {
                "project": {"id": pid, "name": name, "format": fmt},
                "version": {"id": vid, "seq": seq},
                "cue_count": len(cues),
            }
        finally:
            conn.close()


def submit_edit(project_id, payload):
    base_vid = payload.get("base_version")
    note = payload.get("note", "")
    submitted = payload.get("cues")
    if not isinstance(base_vid, int) or not isinstance(submitted, list):
        return 400, {"error": "请求格式不正确：需要 base_version（整数）和 cues（数组）"}
    if not isinstance(note, str):
        note = ""

    norm = []
    for i, c in enumerate(submitted, 1):
        if not isinstance(c, dict):
            return 400, {"error": f"第 {i} 条字幕不是对象"}
        uid = c.get("uid")
        if uid is not None and not isinstance(uid, str):
            return 400, {"error": f"第 {i} 条字幕的 uid 类型错误"}
        try:
            start = int(c["start_ms"])
            end = int(c["end_ms"])
        except (KeyError, TypeError, ValueError):
            return 400, {"error": f"第 {i} 条字幕的 start_ms / end_ms 缺失或不是整数"}
        text = c.get("text")
        if not isinstance(text, str):
            return 400, {"error": f"第 {i} 条字幕的 text 缺失或不是字符串"}
        norm.append({"uid": uid, "start_ms": start, "end_ms": end, "text": text})

    seen = set()
    for c in norm:
        if c["uid"]:
            if c["uid"] in seen:
                return 400, {"error": f"提交中字幕 uid 重复：{c['uid']}"}
            seen.add(c["uid"])

    with _write_lock:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM projects WHERE id = ?",
                                (project_id,)).fetchone():
                conn.rollback()
                return 404, {"error": "项目不存在"}
            base_v = conn.execute(
                "SELECT * FROM versions WHERE id = ? AND project_id = ?",
                (base_vid, project_id),
            ).fetchone()
            if not base_v:
                conn.rollback()
                return 400, {"error": "提交携带的基线版本不存在，请刷新后重试"}
            head = get_head(conn, project_id)

            base_cues = load_cues(conn, base_vid)
            base_uids = {c["uid"] for c in base_cues}
            for c in norm:
                if c["uid"] and c["uid"] not in base_uids:
                    conn.rollback()
                    return 400, {"error": f"字幕 uid 不属于基线版本：{c['uid']}"}

            merged_from = None
            if head["id"] == base_vid:
                merged = norm  # 基线即最新，直接应用
            else:
                head_cues = load_cues(conn, head["id"])
                merged, conflicts = three_way_merge(base_cues, head_cues, norm)
                if conflicts:
                    conn.rollback()
                    return 409, {
                        "error": "conflict",
                        "message": "以下字幕条在你编辑期间也被修改或删除，"
                                   "请逐条人工选择保留哪一方，不会自动覆盖",
                        "head_version": {"id": head["id"], "seq": head["seq"]},
                        "conflicts": conflicts,
                    }
                merged_from = {"id": head["id"], "seq": head["seq"]}

            merged.sort(key=lambda c: (c["start_ms"], c["end_ms"]))
            errors = subtitles.validate(merged)
            if errors:
                conn.rollback()
                return 400, {
                    "error": "validation",
                    "message": "合并结果未通过整轨校验，本次提交已整批拒绝",
                    "reasons": errors,
                }
            vid, seq = insert_version(conn, project_id, note, merged)
            conn.commit()
            return 200, {"version": {"id": vid, "seq": seq},
                         "merged_from": merged_from,
                         "cue_count": len(merged)}
        finally:
            conn.close()


def list_projects():
    conn = db()
    try:
        rows = conn.execute(
            "SELECT p.id, p.name, p.format, p.created_at,"
            " (SELECT MAX(seq) FROM versions v WHERE v.project_id = p.id) AS head_seq"
            " FROM projects p ORDER BY p.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_project(project_id):
    conn = db()
    try:
        p = conn.execute("SELECT * FROM projects WHERE id = ?",
                         (project_id,)).fetchone()
        if not p:
            return None
        head = get_head(conn, project_id)
        return {"project": dict(p),
                "head": {"id": head["id"], "seq": head["seq"],
                         "note": head["note"], "created_at": head["created_at"]},
                "cues": load_cues(conn, head["id"])}
    finally:
        conn.close()


def list_versions(project_id):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT v.id, v.seq, v.note, v.created_at,"
            " (SELECT COUNT(*) FROM cues c WHERE c.version_id = v.id) AS cue_count"
            " FROM versions v WHERE v.project_id = ? ORDER BY v.seq DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_version(version_id):
    conn = db()
    try:
        v = conn.execute("SELECT * FROM versions WHERE id = ?",
                         (version_id,)).fetchone()
        if not v:
            return None
        return {"version": dict(v), "cues": load_cues(conn, version_id)}
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    server_version = "SubtitleCollab/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _url(self):
        """恢复请求目标中的非 ASCII 字符。

        http.server 按 latin-1 解码请求行，浏览器发来的百分号编码不受影响，
        但 curl 等直接发送的原始 UTF-8 字节需要转回。
        """
        try:
            return urlparse(self.path.encode("latin-1").decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            return urlparse(self.path)

    def _index(self):
        with open(os.path.join(BASE_DIR, "static", "index.html"), "rb") as f:
            self._bytes(f.read(), "text/html; charset=utf-8")

    def do_GET(self):
        u = self._url()
        path, qs = u.path, parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._index()
            if path == "/api/projects":
                return self._json({"projects": list_projects()})
            m = re.fullmatch(r"/api/projects/(\d+)", path)
            if m:
                data = get_project(int(m.group(1)))
                return self._json(data) if data else self._json({"error": "项目不存在"}, 404)
            m = re.fullmatch(r"/api/projects/(\d+)/versions", path)
            if m:
                return self._json({"versions": list_versions(int(m.group(1)))})
            m = re.fullmatch(r"/api/versions/(\d+)", path)
            if m:
                data = get_version(int(m.group(1)))
                return self._json(data) if data else self._json({"error": "版本不存在"}, 404)
            m = re.fullmatch(r"/api/versions/(\d+)/export", path)
            if m:
                return self._export(int(m.group(1)), qs)
            self._json({"error": "接口不存在"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 - 兜底，保证服务不挂
            self._json({"error": f"服务器内部错误：{e}"}, 500)

    def do_POST(self):
        u = self._url()
        path, qs = u.path, parse_qs(u.query)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        try:
            if path == "/api/projects":
                status, obj = create_project(qs, body)
                return self._json(obj, status)
            m = re.fullmatch(r"/api/projects/(\d+)/edits", path)
            if m:
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._json({"error": "请求体不是有效的 JSON"}, 400)
                status, obj = submit_edit(int(m.group(1)), payload)
                return self._json(obj, status)
            self._json({"error": "接口不存在"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"服务器内部错误：{e}"}, 500)

    def _export(self, version_id, qs):
        fmt = (qs.get("fmt") or ["srt"])[0].lower()
        if fmt not in ("srt", "vtt"):
            return self._json({"error": "fmt 只能是 srt 或 vtt"}, 400)
        conn = db()
        try:
            v = conn.execute(
                "SELECT v.*, p.name AS pname FROM versions v"
                " JOIN projects p ON p.id = v.project_id WHERE v.id = ?",
                (version_id,),
            ).fetchone()
            if not v:
                return self._json({"error": "版本不存在"}, 404)
            cues = load_cues(conn, version_id)
        finally:
            conn.close()
        text = subtitles.to_srt(cues) if fmt == "srt" else subtitles.to_vtt(cues)
        fname = f"{v['pname']}_v{v['seq']}.{fmt}"
        ctype = "application/x-subrip" if fmt == "srt" else "text/vtt"
        self._bytes(text.encode("utf-8"), ctype + "; charset=utf-8", 200,
                    {"Content-Disposition":
                     "attachment; filename*=UTF-8''" + quote(fname)})


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"字幕协校服务已启动：http://localhost:{PORT}  数据库：{DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
