#!/usr/bin/env python3
"""自托管字幕协校应用（仅标准库）。

启动：python app.py
配置：环境变量 HOST（默认 0.0.0.0）、PORT（默认 8000）、
      DB_PATH（默认 ./data/subtitles.db，启动时自动建库建表）。
"""
from __future__ import annotations

import html
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import subtitle
from store import Store

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "subtitles.db"))
MAX_BODY = 64 * 1024 * 1024

store = Store(DB_PATH)


def h(s) -> str:
    return html.escape(str(s), quote=True)


CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;color:#222;background:#f6f7f9}
header{background:#2c3e50;padding:10px 20px}
header a{color:#fff;text-decoration:none;font-weight:600}
main{max-width:1100px;margin:20px auto;padding:0 16px}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#f0f2f5}
textarea{width:100%;border:1px solid #ccc;border-radius:4px;padding:4px;font-family:inherit}
input{border:1px solid #ccc;border-radius:4px;padding:4px}
input.bad{border-color:#d00;background:#ffecec}
button{cursor:pointer;border:1px solid #bbb;background:#fff;border-radius:4px;padding:5px 10px;margin:2px 0}
button.primary{background:#2c7be5;border-color:#2c7be5;color:#fff}
tr.new td{background:#f2fbf2}
.muted{color:#888;font-weight:normal}
.conflicts{border:2px solid #e0a800;background:#fffbe6;padding:12px;margin:12px 0;border-radius:6px}
.conflict-card{border:1px solid #e0a800;background:#fff;margin:10px 0;padding:10px;border-radius:6px}
.cols{display:flex;gap:10px}
.col{flex:1;border:1px solid #eee;padding:8px;border-radius:4px;background:#fafafa;min-width:0}
.col h4{margin:0 0 6px}
.t{font-family:ui-monospace,monospace;color:#555}
pre{white-space:pre-wrap}
"""


def layout(title: str, body: str) -> bytes:
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{h(title)} · 字幕协校</title><style>{CSS}</style></head><body>"
        "<header><a href='/'>字幕协校</a></header>"
        f"<main>{body}</main></body></html>"
    ).encode("utf-8")


# ---------------- 页面 ----------------

def page_index() -> bytes:
    rows = []
    for p in store.list_projects():
        rows.append(
            f"<tr><td><a href='/projects/{p['id']}'>{h(p['name'])}</a></td>"
            f"<td>{h(p['source_format'].upper())}</td>"
            f"<td>{h(p['created_at'])}</td>"
            f"<td>v{p['head_version']}</td>"
            f"<td><a href='/projects/{p['id']}/history'>历史版本</a> · "
            f"<a href='/projects/{p['id']}/export'>导出</a></td></tr>"
        )
    table = (
        "<table><thead><tr><th>项目</th><th>格式</th><th>创建时间</th>"
        "<th>当前版本</th><th>操作</th></tr></thead>"
        f"<tbody>{''.join(rows) or '<tr><td colspan=5 class=muted>暂无项目，请上传字幕文件</td></tr>'}</tbody></table>"
    )
    body = f"""
<h2>新建项目</h2>
<p><input type='file' id='file' accept='.srt,.vtt'>
<button class='primary' onclick='upload()'>上传并创建项目</button>
<span class='muted'>仅接受 UTF-8 编码的 .srt / .vtt，格式错误将被拒收</span></p>
<h2>项目列表</h2>
{table}
<script>
async function upload(){{
  const f = document.getElementById('file').files[0];
  if(!f){{ alert('请先选择文件'); return; }}
  const r = await fetch('/projects?name=' + encodeURIComponent(f.name),
    {{method:'POST', body:f}});  // 发送原始字节，由服务端严格校验 UTF-8
  if(r.redirected){{ location.href = r.url; return; }}
  const d = await r.json().catch(() => ({{message:'未知错误'}}));
  alert('导入失败：' + (d.message || r.status));
}}
</script>"""
    return layout("项目列表", body)


EDITOR_PAGE = """
<h2>__PNAME__ <span class="muted">（源格式 __FMT__）</span></h2>
<p><b id="status">加载中…</b> ·
<a href="/projects/__PID__/history">历史版本</a> ·
<a href="/projects/__PID__/export">导出当前版本</a> ·
<a href="/">返回列表</a></p>
<p class="muted">时间格式 HH:MM:SS,mmm（逗号、句点均可）。保存即提交合并：
只动了别人没动的字幕条会自动合并；同一条被双方改/删时会要求人工选择，绝不覆盖。</p>
<div id="conflicts" class="conflicts" hidden></div>
<table id="track">
<thead><tr><th>#</th><th>起点</th><th>终点</th><th>文本</th><th>操作</th></tr></thead>
<tbody></tbody>
</table>
<p>
<button onclick="addRow(rows.length)">在末尾新增字幕条</button>
<button class="primary" onclick="save()">保存（提交合并）</button>
<button onclick="loadState()">放弃本地改动并重新加载</button>
</p>
<script>
const PID = __PID__;
let baseVersion = null;
let rows = [];            // {uid|null, startEl, endEl, textEl, tr, idxEl}
let origByUid = {};       // uid -> 载入时的原始值
let pendingDeletes = [];  // 被删除且已有 uid 的字幕条
let lastOps = null, lastConflict = null;
let seq = 0;
const tbody = document.querySelector('#track tbody');

function msToTime(ms){
  const p = (n, l) => String(n).padStart(l, '0');
  return p(Math.floor(ms/3600000),2) + ':' + p(Math.floor(ms%3600000/60000),2)
       + ':' + p(Math.floor(ms%60000/1000),2) + ',' + p(ms%1000,3);
}
function timeToMs(t){
  const m = /^(\\d+):([0-5]\\d):([0-5]\\d)[,.](\\d{3})$/.exec(t.trim());
  if(!m) return null;
  return ((+m[1])*60 + (+m[2]))*60000 + (+m[3])*1000 + (+m[4]);
}
function esc(s){ const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }
function setStatus(msg){ document.getElementById('status').textContent = msg; }

async function loadState(){
  const r = await fetch(`/api/projects/${PID}/state`);
  if(!r.ok){ setStatus('加载失败'); return; }
  const s = await r.json();
  baseVersion = s.head_version;
  rows = []; origByUid = {}; pendingDeletes = [];
  tbody.innerHTML = '';
  for(const e of s.entries){
    origByUid[e.uid] = {start_ms:e.start_ms, end_ms:e.end_ms, text:e.text};
    addRow(rows.length, e.uid, e.start_ms, e.end_ms, e.text);
  }
  hideConflicts();
  setStatus(`当前版本 v${s.head_version}，共 ${s.entries.length} 条`);
}

function addRow(index, uid=null, startMs=null, endMs=null, text=''){
  const row = {uid};
  const tr = document.createElement('tr');
  if(!uid) tr.className = 'new';
  const idxEl = document.createElement('td');
  const tdS = document.createElement('td');
  const inS = document.createElement('input');
  inS.size = 12; inS.placeholder = '00:00:00,000';
  if(startMs !== null) inS.value = msToTime(startMs);
  tdS.appendChild(inS);
  const tdE = document.createElement('td');
  const inE = document.createElement('input');
  inE.size = 12; inE.placeholder = '00:00:00,000';
  if(endMs !== null) inE.value = msToTime(endMs);
  tdE.appendChild(inE);
  const tdT = document.createElement('td');
  const ta = document.createElement('textarea'); ta.rows = 2; ta.value = text;
  tdT.appendChild(ta);
  const tdOps = document.createElement('td');
  const btnIns = document.createElement('button'); btnIns.textContent = '下方插入';
  btnIns.onclick = () => addRow(rows.indexOf(row) + 1);
  const btnDel = document.createElement('button'); btnDel.textContent = '删除';
  btnDel.onclick = () => {
    if(row.uid) pendingDeletes.push(row.uid);
    rows.splice(rows.indexOf(row), 1);
    tr.remove(); renumber();
  };
  tdOps.append(btnIns, btnDel);
  tr.append(idxEl, tdS, tdE, tdT, tdOps);
  Object.assign(row, {startEl:inS, endEl:inE, textEl:ta, tr, idxEl});
  rows.splice(index, 0, row);
  const next = rows[index + 1];
  if(next) tbody.insertBefore(tr, next.tr); else tbody.appendChild(tr);
  renumber();
}

function renumber(){ rows.forEach((r, i) => r.idxEl.textContent = i + 1); }

function buildOps(){
  const ops = pendingDeletes.map(uid => ({type:'delete', uid}));
  let prevUid = null;
  const bad = [];
  rows.forEach((row, i) => {
    const s = timeToMs(row.startEl.value), e = timeToMs(row.endEl.value);
    row.startEl.classList.toggle('bad', s === null);
    row.endEl.classList.toggle('bad', e === null);
    if(s === null || e === null){ bad.push(i + 1); return; }
    const text = row.textEl.value;
    if(row.uid === null){
      ops.push({type:'insert', after_uid:prevUid, start_ms:s, end_ms:e, text});
    } else {
      const o = origByUid[row.uid];
      if(o.start_ms !== s || o.end_ms !== e || o.text !== text)
        ops.push({type:'update', uid:row.uid, start_ms:s, end_ms:e, text});
      prevUid = row.uid;
    }
  });
  if(bad.length){ alert(`第 ${bad.join('、')} 行时间格式不正确（应为 HH:MM:SS,mmm）`); return null; }
  return ops;
}

async function save(){
  const ops = buildOps();
  if(!ops) return;
  if(ops.length === 0){ setStatus('没有改动'); return; }
  lastOps = ops;
  await submit(baseVersion, ops);
}

async function submit(base, ops){
  setStatus('提交中…');
  let r, data;
  try{
    r = await fetch(`/api/projects/${PID}/merge`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({base_version:base, ops})});
    data = await r.json();
  }catch(e){ setStatus('网络错误'); return; }
  if(data.status === 'merged'){
    lastOps = null;
    await loadState();
    setStatus(`已合并为 v${data.version}（${data.note}）`);
  } else if(data.status === 'noop'){
    setStatus('没有需要保存的改动');
  } else if(data.status === 'conflict'){
    setStatus(`检测到 ${data.conflicts.length} 处冲突，请人工选择后重新提交`);
    showConflicts(data);
  } else if(data.status === 'invalid'){
    setStatus('整轨校验未通过，整批已拒绝');
    alert('整轨校验未通过，已整批拒绝：\\n\\n' + data.errors.join('\\n'));
  } else {
    setStatus('提交失败');
    alert('提交失败：' + (data.message || (data.errors || []).join('\\n') || r.status));
  }
}

function fmtEntry(e){
  if(!e) return '<div class="muted">（已删除）</div>';
  return `<div class="t">${esc(msToTime(e.start_ms))} → ${esc(msToTime(e.end_ms))}</div>`
       + `<div>${esc(e.text).replace(/\\n/g, '<br>')}</div>`;
}

function showConflicts(data){
  lastConflict = data;
  const box = document.getElementById('conflicts');
  box.hidden = false;
  box.innerHTML = `<h3>冲突：以下字幕条在你编辑期间（基于 v${baseVersion}）被他人改/删，对方版本为 v${data.head_version}</h3>`;
  const kinds = {
    both_updated:'你和对方都修改了同一条字幕',
    update_vs_delete:'你修改了这条字幕，但对方已将其删除',
    delete_vs_update:'你删除了这条字幕，但对方修改了它',
    anchor_deleted:'你新增字幕的插入锚点（上一条字幕）已被对方删除',
  };
  data.conflicts.forEach((c, i) => {
    const yours = c.kind === 'delete_vs_update' ? '<div class="muted">（删除该条）</div>' : fmtEntry(c.yours);
    const div = document.createElement('div');
    div.className = 'conflict-card';
    div.innerHTML = `<div><b>冲突 ${i+1}</b>（字幕 ${esc(c.uid)}）：${kinds[c.kind] || c.kind}</div>
      <div class="cols">
        <div class="col"><h4>你编辑时的版本</h4>${fmtEntry(c.base)}</div>
        <div class="col"><h4>对方当前版本</h4>${fmtEntry(c.head)}</div>
        <div class="col"><h4>你的修改</h4>${yours}</div>
      </div>
      <label><input type="radio" name="c${i}" value="theirs"> 保留对方</label>
      <label><input type="radio" name="c${i}" value="yours"> 保留我的</label>`;
    box.appendChild(div);
  });
  const bar = document.createElement('p');
  const ok = document.createElement('button');
  ok.className = 'primary'; ok.textContent = '应用选择并重新提交';
  ok.onclick = resolveConflicts;
  const giveUp = document.createElement('button');
  giveUp.textContent = '放弃我的全部改动并加载对方版本';
  giveUp.onclick = () => { lastOps = null; loadState(); };
  bar.append(ok, giveUp);
  box.appendChild(bar);
  box.scrollIntoView();
}

function hideConflicts(){
  const box = document.getElementById('conflicts');
  box.hidden = true; box.innerHTML = '';
  lastConflict = null;
}

function nearestPrevUid(uid){
  const i = rows.findIndex(r => r.uid === uid);
  for(let j = i - 1; j >= 0; j--) if(rows[j].uid) return rows[j].uid;
  return null;
}

async function resolveConflicts(){
  const dropUid = new Set();        // 放弃我对该字幕条的改动
  const dropAnchor = new Set();     // 放弃锚点已删的插入
  const extra = [];
  for(let i = 0; i < lastConflict.conflicts.length; i++){
    const c = lastConflict.conflicts[i];
    const sel = document.querySelector(`input[name="c${i}"]:checked`);
    if(!sel){ alert('请为每个冲突选择「保留对方」或「保留我的」'); return; }
    if(sel.value === 'theirs'){
      (c.kind === 'anchor_deleted' ? dropAnchor : dropUid).add(c.uid);
      continue;
    }
    if(c.kind === 'update_vs_delete'){
      // 对方已删除：以原 uid 重新插入，恢复原字幕条
      extra.push({type:'insert', uid:c.uid, after_uid:nearestPrevUid(c.uid),
                  on_missing_anchor:'end',
                  start_ms:c.yours.start_ms, end_ms:c.yours.end_ms, text:c.yours.text});
      dropUid.add(c.uid);
    } else if(c.kind === 'anchor_deleted'){
      extra.push({type:'insert', after_uid:nearestPrevUid(c.uid), on_missing_anchor:'end',
                  start_ms:c.yours.start_ms, end_ms:c.yours.end_ms, text:c.yours.text});
      dropAnchor.add(c.uid);
    }
    // both_updated / delete_vs_update：原操作直接保留，基于对方版本重放即可
  }
  const ops = lastOps.filter(op => {
    if(op.type === 'insert') return !(op.after_uid && dropAnchor.has(op.after_uid));
    return !dropUid.has(op.uid);
  }).concat(extra);
  if(ops.length === 0){ lastOps = null; await loadState(); setStatus('已放弃我的改动'); return; }
  await submit(lastConflict.head_version, ops);
}

loadState();
</script>
"""


def page_editor(pid: int) -> bytes:
    proj = store.get_project(pid)
    if proj is None:
        return layout("未找到", "<h2>项目不存在</h2>")
    body = (
        EDITOR_PAGE.replace("__PID__", str(pid))
        .replace("__PNAME__", h(proj["name"]))
        .replace("__FMT__", h(proj["source_format"].upper()))
    )
    return layout(proj["name"], body)


def page_history(pid: int) -> bytes:
    proj = store.get_project(pid)
    if proj is None:
        return layout("未找到", "<h2>项目不存在</h2>")
    rows = []
    for v in store.list_versions(pid):
        n = v["number"]
        rows.append(
            f"<tr><td>v{n}</td><td>{h(v['created_at'])}</td><td>{h(v['note'])}</td>"
            f"<td>{v['entry_count']}</td>"
            f"<td><a href='/projects/{pid}/versions/{n}'>查看</a> · "
            f"<a href='/projects/{pid}/export?version={n}&format=srt'>导出 SRT</a> · "
            f"<a href='/projects/{pid}/export?version={n}&format=vtt'>导出 VTT</a></td></tr>"
        )
    body = f"""
<h2>{h(proj['name'])} · 历史版本</h2>
<p><a href='/projects/{pid}'>返回编辑</a> · <a href='/'>返回列表</a></p>
<table><thead><tr><th>版本</th><th>时间</th><th>说明</th><th>字幕条数</th><th>操作</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>"""
    return layout(f"{proj['name']} · 历史版本", body)


def page_version(pid: int, number: int) -> bytes:
    proj = store.get_project(pid)
    entries = store.snapshot(pid, number)
    info = store.version_info(pid, number)
    if proj is None or entries is None:
        return layout("未找到", "<h2>项目或版本不存在</h2>")
    rows = []
    for i, e in enumerate(entries, 1):
        rows.append(
            f"<tr><td>{i}</td><td class='t'>{h(subtitle.ms_to_srt(e['start_ms']))}</td>"
            f"<td class='t'>{h(subtitle.ms_to_srt(e['end_ms']))}</td>"
            f"<td>{h(e['text']).replace(chr(10), '<br>')}</td>"
            f"<td class='muted'>{h(e['uid'])}</td></tr>"
        )
    body = f"""
<h2>{h(proj['name'])} · v{number} <span class="muted">（只读快照）</span></h2>
<p>{h(info['note'])} · {h(info['created_at'])}</p>
<p><a href='/projects/{pid}/export?version={number}&format=srt'>导出 SRT</a> ·
<a href='/projects/{pid}/export?version={number}&format=vtt'>导出 VTT</a> ·
<a href='/projects/{pid}/history'>历史版本</a> ·
<a href='/projects/{pid}'>返回编辑</a></p>
<table><thead><tr><th>#</th><th>起点</th><th>终点</th><th>文本</th><th>ID</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>"""
    return layout(f"{proj['name']} · v{number}", body)


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SubEdit/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def do_GET(self):
        self._body_bytes = b""
        self._route("GET")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._body_bytes = b""
            return self._json(413, {"message": "请求体过大"})
        self._body_bytes = self.rfile.read(length) if length else b""
        self._route("POST")

    def _route(self, method: str):
        try:
            path = urlparse(self.path).path
            q = parse_qs(urlparse(self.path).query)
            if method == "GET" and path == "/":
                return self._send(200, page_index())
            if method == "POST" and path == "/projects":
                return self._create_project(q)
            m = re.fullmatch(r"/projects/(\d+)", path)
            if m and method == "GET":
                return self._send(200, page_editor(int(m.group(1))))
            m = re.fullmatch(r"/projects/(\d+)/history", path)
            if m and method == "GET":
                return self._send(200, page_history(int(m.group(1))))
            m = re.fullmatch(r"/projects/(\d+)/versions/(\d+)", path)
            if m and method == "GET":
                return self._send(200, page_version(int(m.group(1)), int(m.group(2))))
            m = re.fullmatch(r"/projects/(\d+)/export", path)
            if m and method == "GET":
                return self._export(int(m.group(1)), q)
            m = re.fullmatch(r"/api/projects/(\d+)/state", path)
            if m and method == "GET":
                return self._api_state(int(m.group(1)))
            m = re.fullmatch(r"/api/projects/(\d+)/merge", path)
            if m and method == "POST":
                return self._api_merge(int(m.group(1)))
            self._send(404, layout("未找到", "<h2>404 未找到</h2>"))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接直接断开
            try:
                self._send(500, layout("服务器错误", f"<h2>500 服务器错误</h2><pre>{h(exc)}</pre>"))
            except Exception:
                pass

    # ---- 响应助手 ----

    def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- 接口 ----

    def _create_project(self, q):
        try:
            text = self._body_bytes.decode("utf-8")  # 严格校验 UTF-8
        except UnicodeDecodeError:
            return self._json(400, {"message": "文件不是有效的 UTF-8 编码"})
        name = (q.get("name", [""])[0] or "未命名").strip() or "未命名"
        fmt = subtitle.detect_format(name, text)
        name = re.sub(r"\.(srt|vtt)$", "", name, flags=re.IGNORECASE) or name
        try:
            entries = subtitle.parse(text, fmt)
        except subtitle.ParseError as exc:
            return self._json(400, {"message": f"字幕文件格式错误：{exc}"})
        pid = store.create_project(name, fmt, entries)  # 解析成功才落库
        self._redirect(f"/projects/{pid}")

    def _api_state(self, pid: int):
        proj = store.get_project(pid)
        if proj is None:
            return self._json(404, {"message": "项目不存在"})
        head_num, entries = store.head(pid)
        self._json(200, {
            "project": {"id": pid, "name": proj["name"], "source_format": proj["source_format"]},
            "head_version": head_num,
            "entries": entries,
        })

    def _api_merge(self, pid: int):
        try:
            payload = json.loads(self._body_bytes.decode("utf-8"))
        except Exception:
            return self._json(400, {"message": "请求体不是合法 JSON"})
        base, ops = payload.get("base_version"), payload.get("ops")
        if not isinstance(base, int) or isinstance(base, bool) or not isinstance(ops, list):
            return self._json(400, {"message": "需要 base_version（整数）与 ops（数组）"})
        if store.get_project(pid) is None:
            return self._json(404, {"message": "项目不存在"})
        result = store.merge(pid, base, ops)
        code = {"merged": 200, "noop": 200, "conflict": 409, "invalid": 422}.get(
            result["status"], 400)
        self._json(code, result)

    def _export(self, pid: int, q):
        proj = store.get_project(pid)
        if proj is None:
            return self._send(404, layout("未找到", "<h2>项目不存在</h2>"))
        head_num, _ = store.head(pid)
        try:
            version = int(q.get("version", [str(head_num)])[0])
        except ValueError:
            return self._json(400, {"message": "version 参数非法"})
        fmt = q.get("format", [proj["source_format"]])[0]
        if fmt not in ("srt", "vtt"):
            return self._json(400, {"message": "format 仅支持 srt / vtt"})
        entries = store.snapshot(pid, version)
        if entries is None:
            return self._send(404, layout("未找到", f"<h2>版本 v{version} 不存在</h2>"))
        body = subtitle.to_srt(entries) if fmt == "srt" else subtitle.to_vtt(entries)
        filename = f"{proj['name']}-v{version}.{fmt}"
        self._send(200, body.encode("utf-8"), "text/plain; charset=utf-8",
                   {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"字幕协校已启动：http://localhost:{PORT} （数据库：{DB_PATH}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
