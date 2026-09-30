#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据配音文案 + 分段音频，生成 SRT 字幕。

思路：TTS 不提供逐字时间戳，所以按「每段音频总时长」除以「该段文字总字数」
得到平均语速，再按每条字幕的字数分配时间。中文语速稳定，实测误差可接受。

用法:
    python scripts/make_subtitles.py outputs/tutorial/配音文案.md
"""

import argparse
import os
import re
import subprocess
import sys

MAX_CHARS = 20          # 单条字幕目标字数
SOFT_LIMIT = 1.5        # 允许超长到这个倍数（宁可略长，也别把词组切碎）
MIN_CHARS = 7           # 短于这个长度的碎片并入上一条，避免一闪而过
TAIL_TRIM = 0.35        # 每段末尾留白（TTS 尾部有约 0.4s 静音），不计入字幕时间

# 句末标点：优先在这里断开
SENT_END = "。！？!?…"
# 句中停顿：长句在逗号处再断
CLAUSE = "，、；：,;"


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True).stdout.decode().strip()
    return float(out)


def parse_sections(md_path):
    with open(md_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    sections, cur = [], None
    for ln in lines:
        if ln.startswith("## "):
            if cur:
                sections.append(cur)
            cur = {"title": ln[3:].strip(), "body": []}
        elif cur is not None:
            if ln.strip() == "---":
                continue
            cur["body"].append(ln)
    if cur:
        sections.append(cur)

    out = []
    for s in sections:
        text = re.sub(r"[ \t]+", " ", "\n".join(s["body"]).strip()).replace("\n", "")
        if text:
            out.append((s["title"], text))
    return out


def _join(a, b):
    """拼接字幕碎片。

    ⚠️ 硬切可能正好断在空格处，直接相加会把英文单词粘在一起
    （"diffusion" + "cpp" -> "diffusioncpp"）。两侧都是 ASCII 字母数字时补回空格。
    """
    if a and b and a[-1].isascii() and a[-1].isalnum() \
            and b[0].isascii() and b[0].isalnum():
        return a + " " + b
    return a + b


def _hard_split(s, limit):
    """没有标点可断时，优先在空格处断，最后才硬切。"""
    out = []
    while len(s) > limit * SOFT_LIMIT:
        cut = s.rfind(" ", 0, int(limit * SOFT_LIMIT))
        if cut < limit // 2:
            cut = int(limit * SOFT_LIMIT)
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def split_chunks(text, limit=MAX_CHARS):
    """把一段话切成适合做字幕的短句。

    原则：**宁可单条略长，也不要把词组切碎** —— 字幕一闪而过比略长更难读。
    所以只有明显超长（超过 SOFT_LIMIT 倍）才继续拆。
    """
    parts = re.split(rf"(?<=[{SENT_END}])", text)
    chunks = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= limit * SOFT_LIMIT:
            chunks.append(p)
            continue
        subs = re.split(rf"(?<=[{CLAUSE}])", p)
        buf = ""
        for s in subs:
            if len(buf) + len(s) <= limit:
                buf += s
            else:
                if buf:
                    chunks.append(buf.strip())
                buf = s
        if buf.strip():
            chunks.append(buf.strip())

    # 硬切仍然过长的
    final = []
    for c in chunks:
        if len(c) > limit * SOFT_LIMIT:
            final.extend(_hard_split(c, limit))
        else:
            final.append(c)

    # 过短的碎片并入上一条
    merged = []
    for c in final:
        if merged and len(c) < MIN_CHARS:
            merged[-1] = _join(merged[-1], c)
        else:
            merged.append(c)
    return [c for c in merged if c]


def ts(sec):
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def main():
    ap = argparse.ArgumentParser(description="生成 SRT 字幕")
    ap.add_argument("markdown", help="配音文案（Markdown，## 分节）")
    ap.add_argument("--audio-dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    md = os.path.abspath(args.markdown)
    base = os.path.dirname(md)
    audio_dir = args.audio_dir or os.path.join(base, "audio")
    out_srt = args.out or os.path.join(base, "字幕.srt")

    sections = parse_sections(md)
    audios = sorted(f for f in os.listdir(audio_dir)
                    if f.endswith(".mp3") and not f.startswith("完整"))
    if len(sections) != len(audios):
        print(f"[警告] 文案 {len(sections)} 节，音频 {len(audios)} 段")

    entries, clock = [], 0.0
    for i, (title, text) in enumerate(sections):
        if i >= len(audios):
            break
        dur = probe_duration(os.path.join(audio_dir, audios[i])) - TAIL_TRIM
        if dur <= 0:
            continue
        chunks = split_chunks(text)
        total_chars = sum(len(c) for c in chunks) or 1
        # 按字数比例分配时间
        t = clock
        for c in chunks:
            span = dur * len(c) / total_chars
            entries.append((t, t + span, c))
            t += span
        clock += probe_duration(os.path.join(audio_dir, audios[i]))
        print(f"  [{i+1}] {title:<22} {dur:6.2f}s  -> {len(chunks):2d} 条字幕")

    with open(out_srt, "w", encoding="utf-8") as fh:
        for n, (a, b, txt) in enumerate(entries, 1):
            fh.write(f"{n}\n{ts(a)} --> {ts(b)}\n{txt}\n\n")

    print(f"\n完成: {out_srt}")
    print(f"      {len(entries)} 条字幕，总时长 {clock:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
