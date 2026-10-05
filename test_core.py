"""核心逻辑单元测试：解析、校验、三方合并。运行：python -m unittest -v"""
import os
import tempfile
import unittest

import subtitle
from store import Store

SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n甲\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\n乙\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\n丙\n"
)

VTT = (
    "WEBVTT - 示例\n\n"
    "NOTE 这是一段注释\n\n"
    "cue-1\n00:01.000 --> 00:02.000\n你好\n\n"
    "00:03.000 --> 00:04.500 align:start\n世界\n"
)


class ParseTests(unittest.TestCase):
    def test_srt_ok(self):
        entries = subtitle.parse_srt(SRT)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0], {"start_ms": 1000, "end_ms": 2000, "text": "甲"})
        self.assertEqual(entries[2]["end_ms"], 6000)

    def test_vtt_ok(self):
        entries = subtitle.parse_vtt(VTT)
        self.assertEqual(len(entries), 2)  # NOTE 块被跳过
        self.assertEqual(entries[0]["start_ms"], 1000)
        self.assertEqual(entries[1]["end_ms"], 4500)
        self.assertEqual(entries[1]["text"], "世界")

    def test_roundtrip(self):
        entries = subtitle.parse_srt(SRT)
        again = subtitle.parse_srt(subtitle.to_srt(entries))
        self.assertEqual(entries, again)
        vtt_entries = subtitle.parse_vtt(subtitle.to_vtt(entries))
        self.assertEqual(entries, vtt_entries)

    def test_reject_bad(self):
        bad_cases = [
            "",                                             # 空文件
            "1\n00:00:01.000 --> 00:00:02.000\n点号时间\n",  # SRT 用了句点
            "1\n00:00:01,000 00:00:02,000\n缺箭头\n",        # 缺少 -->
            "abc\n00:00:01,000 --> 00:00:02,000\n序号非数字\n",
            "1\n00:00:02,000 --> 00:00:01,000\n终点早于起点\n",
            "1\n00:00:01,000 --> 00:00:02,000\n",            # 缺文本
            "00:61:01,000 --> 00:61:02,000\n1\n分钟越界\n",
        ]
        for text in bad_cases:
            with self.assertRaises(subtitle.ParseError, msg=text):
                subtitle.parse_srt(text)

    def test_reject_bad_vtt(self):
        with self.assertRaises(subtitle.ParseError):
            subtitle.parse_vtt("1\n00:01.000 --> 00:02.000\n没有 WEBVTT 头\n")
        with self.assertRaises(subtitle.ParseError):
            subtitle.parse_vtt("WEBVTT\n\n00:01,000 --> 00:02,000\n逗号时间\n")

    def test_detect_format(self):
        self.assertEqual(subtitle.detect_format("a.vtt", ""), "vtt")
        self.assertEqual(subtitle.detect_format("a.srt", ""), "srt")
        self.assertEqual(subtitle.detect_format("a.txt", "WEBVTT\n\n"), "vtt")
        self.assertEqual(subtitle.detect_format("a.txt", SRT), "srt")


class ValidateTests(unittest.TestCase):
    def test_ok(self):
        entries = [
            {"uid": "a", "start_ms": 0, "end_ms": 1000, "text": "x"},
            {"uid": "b", "start_ms": 1000, "end_ms": 2000, "text": "y"},  # 首尾相接不算重叠
        ]
        self.assertEqual(subtitle.validate_track(entries), [])

    def test_errors(self):
        entries = [
            {"uid": "a", "start_ms": -5, "end_ms": 1000, "text": "负"},
            {"uid": "b", "start_ms": 3000, "end_ms": 3000, "text": "零长"},
            {"uid": "c", "start_ms": 500, "end_ms": 1500, "text": "重叠"},
        ]
        errors = subtitle.validate_track(entries)
        self.assertEqual(len(errors), 3)
        joined = "\n".join(errors)
        self.assertIn("起点为负", joined)
        self.assertIn("不晚于起点", joined)
        self.assertIn("重叠", joined)


class MergeTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        self.store = Store(self.db)
        self.pid = self.store.create_project("测试", "srt", subtitle.parse_srt(SRT))
        _, entries = self.store.head(self.pid)
        self.u1, self.u2, self.u3 = [e["uid"] for e in entries]

    def tearDown(self):
        os.unlink(self.db)

    def upd(self, uid, text, start=None, end=None):
        e = next(x for x in self.store.head(self.pid)[1] if x["uid"] == uid)
        return {"type": "update", "uid": uid, "text": text,
                "start_ms": start if start is not None else e["start_ms"],
                "end_ms": end if end is not None else e["end_ms"]}

    def test_merge_non_conflicting(self):
        r1 = self.store.merge(self.pid, 1, [self.upd(self.u1, "甲改")])
        self.assertEqual(r1["status"], "merged")
        # 另一用户基于 v1 改另一条：应自动合并
        base2 = [dict(e) for e in self.store.snapshot(self.pid, 1)]
        e2 = next(e for e in base2 if e["uid"] == self.u2)
        r2 = self.store.merge(self.pid, 1, [
            {"type": "update", "uid": self.u2, "text": "乙改",
             "start_ms": e2["start_ms"], "end_ms": e2["end_ms"]}])
        self.assertEqual(r2["status"], "merged")
        _, final = self.store.head(self.pid)
        texts = {e["uid"]: e["text"] for e in final}
        self.assertEqual(texts[self.u1], "甲改")
        self.assertEqual(texts[self.u2], "乙改")
        self.assertEqual(texts[self.u3], "丙")

    def test_conflict_same_entry(self):
        self.store.merge(self.pid, 1, [self.upd(self.u1, "甲改")])
        r = self.store.merge(self.pid, 1, [
            {"type": "update", "uid": self.u1, "text": "甲改改",
             "start_ms": 1000, "end_ms": 2000}])
        self.assertEqual(r["status"], "conflict")
        c = r["conflicts"][0]
        self.assertEqual(c["kind"], "both_updated")
        self.assertEqual(c["base"]["text"], "甲")
        self.assertEqual(c["head"]["text"], "甲改")
        self.assertEqual(c["yours"]["text"], "甲改改")
        # 冲突不落库：head 仍是 v2
        self.assertEqual(self.store.head(self.pid)[0], 2)
        # 人工选择"保留我的"后基于 v2 重放：成功
        r2 = self.store.merge(self.pid, r["head_version"], [
            {"type": "update", "uid": self.u1, "text": "甲改改",
             "start_ms": 1000, "end_ms": 2000}])
        self.assertEqual(r2["status"], "merged")

    def test_conflict_update_vs_delete(self):
        self.store.merge(self.pid, 1, [{"type": "delete", "uid": self.u2}])
        r = self.store.merge(self.pid, 1, [
            {"type": "update", "uid": self.u2, "text": "乙改",
             "start_ms": 3000, "end_ms": 4000}])
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflicts"][0]["kind"], "update_vs_delete")
        self.assertIsNone(r["conflicts"][0]["head"])
        # 人工选择"保留我的"：以显式 uid 重新插入恢复
        r2 = self.store.merge(self.pid, r["head_version"], [
            {"type": "insert", "uid": self.u2, "after_uid": self.u1,
             "start_ms": 3000, "end_ms": 4000, "text": "乙改"}])
        self.assertEqual(r2["status"], "merged")
        _, final = self.store.head(self.pid)
        self.assertIn(self.u2, [e["uid"] for e in final])

    def test_conflict_delete_vs_update(self):
        self.store.merge(self.pid, 1, [self.upd(self.u3, "丙改")])
        r = self.store.merge(self.pid, 1, [{"type": "delete", "uid": self.u3}])
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflicts"][0]["kind"], "delete_vs_update")

    def test_insert_and_delete_merge(self):
        r = self.store.merge(self.pid, 1, [
            {"type": "insert", "after_uid": self.u1,
             "start_ms": 2000, "end_ms": 3000, "text": "新"},
            {"type": "delete", "uid": self.u3},
        ])
        self.assertEqual(r["status"], "merged")
        _, final = self.store.head(self.pid)
        self.assertEqual([e["text"] for e in final], ["甲", "新", "乙"])

    def test_invalid_overlap_rejected(self):
        r = self.store.merge(self.pid, 1, [
            {"type": "update", "uid": self.u2, "text": "乙",
             "start_ms": 1500, "end_ms": 2500}])  # 与 u1 (1000-2000) 重叠
        self.assertEqual(r["status"], "invalid")
        self.assertTrue(any("重叠" in e for e in r["errors"]))
        self.assertEqual(self.store.head(self.pid)[0], 1)  # 整批拒绝，无新版本

    def test_invalid_negative_rejected(self):
        r = self.store.merge(self.pid, 1, [
            {"type": "update", "uid": self.u1, "text": "甲",
             "start_ms": -100, "end_ms": 2000}])
        self.assertEqual(r["status"], "invalid")
        self.assertTrue(any("起点为负" in e for e in r["errors"]))

    def test_base_version_missing(self):
        r = self.store.merge(self.pid, 99, [self.upd(self.u1, "x")])
        self.assertEqual(r["status"], "error")

    def test_old_version_snapshot_kept(self):
        self.store.merge(self.pid, 1, [self.upd(self.u1, "甲改")])
        v1 = self.store.snapshot(self.pid, 1)
        self.assertEqual([e["text"] for e in v1], ["甲", "乙", "丙"])
        srt = subtitle.to_srt(v1)
        self.assertIn("00:00:01,000 --> 00:00:02,000", srt)

    def test_same_change_not_conflict(self):
        self.store.merge(self.pid, 1, [self.upd(self.u1, "一样的修改")])
        r = self.store.merge(self.pid, 1, [
            {"type": "update", "uid": self.u1, "text": "一样的修改",
             "start_ms": 1000, "end_ms": 2000}])
        self.assertEqual(r["status"], "merged")


if __name__ == "__main__":
    unittest.main()
