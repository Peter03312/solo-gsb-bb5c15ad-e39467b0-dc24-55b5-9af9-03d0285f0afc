"""SRT/VTT 解析、序列化与整轨校验。

解析是严格的：任何格式错误都抛出 ParseError，调用方据此拒收文件。
"""
import re


class ParseError(Exception):
    pass


# SRT: HH:MM:SS,mmm（也容忍小数点），毫秒必须恰好 3 位
_SRT_TS = re.compile(r"^(\d+):(\d{2}):(\d{2})[,.](\d{3})$")
# VTT: [HH:]MM:SS.mmm
_VTT_TS = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})\.(\d{3})$")
_TIMING = re.compile(r"^(\S+)\s+-->\s+(\S+)(?:\s+.*)?$")


def _ts_ms(token, rx, block_no):
    m = rx.match(token)
    if not m:
        raise ParseError(
            f"第 {block_no} 个字幕块的时间戳无效：{token!r}"
            "（应为 HH:MM:SS,mmm 形式，毫秒为 3 位数字）"
        )
    h, mi, s, ms = m.groups()
    h = int(h or 0)
    mi = int(mi)
    s = int(s)
    ms = int(ms)
    if s >= 60 or (rx is _SRT_TS and mi >= 60) or (rx is _VTT_TS and h and mi >= 60):
        raise ParseError(f"第 {block_no} 个字幕块的时间戳超出范围：{token!r}")
    return ((h * 60 + mi) * 60 + s) * 1000 + ms


def _parse_timing(line, rx, block_no):
    m = _TIMING.match(line.strip())
    if not m:
        raise ParseError(f"第 {block_no} 个字幕块的时间行无效：{line.strip()!r}")
    return _ts_ms(m.group(1), rx, block_no), _ts_ms(m.group(2), rx, block_no)


def parse_srt(text):
    cues = []
    blocks = re.split(r"\n[ \t]*\n", text.strip()) if text.strip() else []
    for bi, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 2:
            raise ParseError(f"第 {bi} 个字幕块不完整（至少需要序号行和时间行）")
        if not lines[0].strip().isdigit():
            raise ParseError(f"第 {bi} 个字幕块的序号行无效：{lines[0].strip()!r}")
        start, end = _parse_timing(lines[1], _SRT_TS, bi)
        body = "\n".join(lines[2:]).strip()
        if not body:
            raise ParseError(f"第 {bi} 个字幕块缺少文本")
        cues.append({"start_ms": start, "end_ms": end, "text": body})
    if not cues:
        raise ParseError("文件中没有字幕条")
    return cues


def parse_vtt(text):
    first = text.split("\n", 1)[0].strip()
    if not (first == "WEBVTT" or first.startswith("WEBVTT ") or first.startswith("WEBVTT\t")):
        raise ParseError("VTT 文件必须以 WEBVTT 头开头")
    body = text.split("\n", 1)[1].strip() if "\n" in text else ""
    cues = []
    if body:
        for block in re.split(r"\n[ \t]*\n", body):
            blines = block.split("\n")
            head = blines[0].strip()
            if head == "STYLE" or head == "REGION" or head == "NOTE" or head.startswith("NOTE "):
                continue
            ti = None
            for i, line in enumerate(blines[:2]):  # 允许一行可选的 cue 标识符
                if "-->" in line:
                    ti = i
                    break
            if ti is None:
                raise ParseError(f"存在无法识别的字幕块：{blines[0][:40]!r}")
            start, end = _parse_timing(blines[ti], _VTT_TS, len(cues) + 1)
            text_body = "\n".join(blines[ti + 1:]).strip()
            if not text_body:
                raise ParseError(f"第 {len(cues) + 1} 个字幕块缺少文本")
            cues.append({"start_ms": start, "end_ms": end, "text": text_body})
    if not cues:
        raise ParseError("文件中没有字幕条")
    return cues


def parse(text, filename=""):
    """解析上传内容，返回 (格式, 字幕条列表)。格式错误抛 ParseError。"""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ParseError("文件内容为空")
    first = text.split("\n", 1)[0].strip()
    if first == "WEBVTT" or first.startswith("WEBVTT ") or first.startswith("WEBVTT\t"):
        return "vtt", parse_vtt(text)
    if filename.lower().endswith(".vtt"):
        return "vtt", parse_vtt(text)
    return "srt", parse_srt(text)


def _fmt(ms, sep):
    if ms < 0:
        ms = 0
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{milli:03d}"


def to_srt(cues):
    out = []
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{_fmt(c['start_ms'], ',')} --> {_fmt(c['end_ms'], ',')}")
        out.extend(c["text"].split("\n"))
        out.append("")
    return "\n".join(out)


def to_vtt(cues):
    out = ["WEBVTT", ""]
    for c in cues:
        out.append(f"{_fmt(c['start_ms'], '.')} --> {_fmt(c['end_ms'], '.')}")
        out.extend(c["text"].split("\n"))
        out.append("")
    return "\n".join(out)


def validate(cues):
    """整轨校验：时间非负、起点早于终点、文本非空、字幕条互不重叠。

    返回错误说明列表，空列表表示通过。
    """
    errors = []
    ordered = sorted(cues, key=lambda c: (c["start_ms"], c["end_ms"]))
    prev = None
    for i, c in enumerate(ordered):
        label = (f"第 {i + 1} 条"
                 f"（{_fmt(max(c['start_ms'], 0), ',')} --> {_fmt(max(c['end_ms'], 0), ',')}）：")
        if c["start_ms"] < 0 or c["end_ms"] < 0:
            errors.append(label + "时间不能为负")
        if c["start_ms"] >= c["end_ms"]:
            errors.append(label + "起点必须早于终点")
        if not str(c["text"]).strip():
            errors.append(label + "文本不能为空")
        if prev is not None and c["start_ms"] < prev["end_ms"]:
            errors.append(label + "与上一条字幕时间重叠")
        prev = c
    return errors
