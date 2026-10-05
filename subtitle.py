"""SRT / VTT 字幕解析、序列化与整轨校验（仅标准库）。"""
from __future__ import annotations

import re


class ParseError(Exception):
    """字幕文件格式错误（拒收，不创建项目）。"""


_SRT_TS = re.compile(r"^(\d{2}):([0-5]\d):([0-5]\d),(\d{3})$")
_VTT_TS = re.compile(r"^(?:(\d{2,}):)?([0-5]\d):([0-5]\d)\.(\d{3})$")
_ARROW = re.compile(r"\s*-->\s*")


def _ms(h, m, s, milli):
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(milli)


def _fmt(ms: int, sep: str) -> str:
    sign = "-" if ms < 0 else ""
    ms = abs(int(ms))
    hh, rem = divmod(ms, 3600000)
    mm, rem = divmod(rem, 60000)
    ss, milli = divmod(rem, 1000)
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}{sep}{milli:03d}"


def ms_to_srt(ms: int) -> str:
    return _fmt(ms, ",")


def ms_to_vtt(ms: int) -> str:
    return _fmt(ms, ".")


def _parse_srt_ts(token: str) -> int:
    m = _SRT_TS.match(token)
    if not m:
        raise ParseError(f"非法 SRT 时间戳 {token!r}（应为 HH:MM:SS,mmm）")
    return _ms(*m.groups())


def _parse_vtt_ts(token: str) -> int:
    m = _VTT_TS.match(token)
    if not m:
        raise ParseError(f"非法 VTT 时间戳 {token!r}（应为 MM:SS.mmm 或 HH:MM:SS.mmm）")
    h, mi, s, milli = m.groups()
    return _ms(int(h) if h else 0, mi, s, milli)


def _normalize(text: str) -> str:
    text = text.lstrip("﻿")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _blocks(text: str):
    text = text.strip()
    if not text:
        raise ParseError("文件为空")
    return [b for b in re.split(r"\n[ \t]*\n", text) if b.strip()]


def _timing(line: str, parse_ts, label: str):
    parts = _ARROW.split(line, maxsplit=1)
    if len(parts) != 2:
        raise ParseError(f"{label}：缺少 '-->' 时间行")
    left, right = parts[0].strip(), parts[1].strip()
    if not left or re.search(r"\s", left):
        raise ParseError(f"{label}：起点时间戳非法 {left!r}")
    tokens = right.split()
    if not tokens:
        raise ParseError(f"{label}：缺少终点时间戳")
    start, end = parse_ts(left), parse_ts(tokens[0])  # 终点后允许跟随设置项，忽略
    if start < 0:
        raise ParseError(f"{label}：起点为负")
    if end <= start:
        raise ParseError(f"{label}：终点 {right!r} 不晚于起点 {left!r}")
    return start, end


def parse_srt(text: str):
    """解析 SRT，返回 [{'start_ms','end_ms','text'}, ...]；格式错误抛 ParseError。"""
    out = []
    for i, block in enumerate(_blocks(_normalize(text)), 1):
        label = f"第 {i} 块"
        lines = block.split("\n")
        if len(lines) < 2:
            raise ParseError(f"{label}：缺少序号或时间行")
        if not re.fullmatch(r"\d+", lines[0].strip()):
            raise ParseError(f"{label}：序号应为纯数字，实际为 {lines[0].strip()!r}")
        start, end = _timing(lines[1], _parse_srt_ts, label)
        cue_text = "\n".join(lines[2:]).strip("\n")
        if not cue_text.strip():
            raise ParseError(f"{label}：缺少字幕文本")
        out.append({"start_ms": start, "end_ms": end, "text": cue_text})
    if not out:
        raise ParseError("未解析到任何字幕条")
    return out


def parse_vtt(text: str):
    """解析 WebVTT；格式错误抛 ParseError。"""
    blocks = _blocks(_normalize(text))
    first = blocks[0].split("\n")[0]
    if not re.fullmatch(r"WEBVTT([ \t].*)?", first):
        raise ParseError("VTT 文件必须以 WEBVTT 开头")
    out = []
    for i, block in enumerate(blocks[1:], 1):
        lines = block.split("\n")
        if lines[0].startswith("NOTE") and (lines[0] == "NOTE" or lines[0][4] in " \t"):
            continue  # 注释块，跳过
        label = f"第 {i} 个 cue"
        if "-->" in lines[0]:
            timing_line, body = lines[0], lines[1:]
        elif len(lines) >= 2 and "-->" in lines[1]:
            timing_line, body = lines[1], lines[2:]  # 首行为 cue 标识符
        else:
            raise ParseError(f"{label}：缺少 '-->' 时间行")
        start, end = _timing(timing_line, _parse_vtt_ts, label)
        cue_text = "\n".join(body).strip("\n")
        if not cue_text.strip():
            raise ParseError(f"{label}：缺少字幕文本")
        out.append({"start_ms": start, "end_ms": end, "text": cue_text})
    if not out:
        raise ParseError("未解析到任何字幕条")
    return out


def detect_format(filename: str, text: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".vtt"):
        return "vtt"
    if name.endswith(".srt"):
        return "srt"
    return "vtt" if _normalize(text).lstrip().startswith("WEBVTT") else "srt"


def parse(text: str, fmt: str):
    return parse_vtt(text) if fmt == "vtt" else parse_srt(text)


def to_srt(entries) -> str:
    chunks = []
    for i, e in enumerate(entries, 1):
        chunks.append(
            f"{i}\n{ms_to_srt(e['start_ms'])} --> {ms_to_srt(e['end_ms'])}\n{e['text']}"
        )
    return "\n\n".join(chunks) + "\n"


def to_vtt(entries) -> str:
    chunks = ["WEBVTT"]
    for e in entries:
        chunks.append(f"{ms_to_vtt(e['start_ms'])} --> {ms_to_vtt(e['end_ms'])}\n{e['text']}")
    return "\n\n".join(chunks) + "\n"


def validate_track(entries) -> list:
    """整轨校验：时间非负、起点早于终点、任意两条不重叠。返回中文错误列表。"""
    errors = []
    for i, e in enumerate(entries, 1):
        label = f"第 {i} 条（{e.get('uid', '?')}）"
        if e["start_ms"] < 0:
            errors.append(f"{label} 起点为负：{ms_to_srt(e['start_ms'])}")
        if e["end_ms"] <= e["start_ms"]:
            errors.append(
                f"{label} 终点 {ms_to_srt(e['end_ms'])} 不晚于起点 {ms_to_srt(e['start_ms'])}"
            )
    ordered = sorted(entries, key=lambda e: (e["start_ms"], e["end_ms"]))
    for prev, cur in zip(ordered, ordered[1:]):
        if cur["start_ms"] < prev["end_ms"]:
            errors.append(
                "字幕条重叠："
                f"{ms_to_srt(prev['start_ms'])}–{ms_to_srt(prev['end_ms'])}（{prev.get('uid', '?')}）"
                f" 与 {ms_to_srt(cur['start_ms'])}–{ms_to_srt(cur['end_ms'])}（{cur.get('uid', '?')}）"
            )
    return errors
