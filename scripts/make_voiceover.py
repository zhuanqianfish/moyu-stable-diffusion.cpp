#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把配音文案（Markdown，用 `## ` 分节）转成语音。

用的是 tts.wangwangit.com 的 OpenAI 兼容接口：
    POST /v1/audio/speech   {input, voice, speed, pitch, style}

⚠️ 该服务会校验来源，必须带 Origin / Referer，否则返回 403 Forbidden。

默认音色：晓辰 Xiaochen（zh-CN-XiaochenNeural），语速 1.15。

用法:
    python scripts/make_voiceover.py outputs/tutorial/配音文案.md
    python scripts/make_voiceover.py 文案.md --voice zh-CN-XiaoxiaoNeural --speed 1.0
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://tts.wangwangit.com/v1/audio/speech"
REFERER = "https://tts.wangwangit.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

DEFAULT_VOICE = "zh-CN-XiaochenNeural"   # 晓辰 Xiaochen (女声·知性)
DEFAULT_SPEED = 1.15


def parse_sections(md_path):
    """按 `## ` 标题切分文案；跳过开头的元信息。"""
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
        text = "\n".join(s["body"]).strip()
        text = re.sub(r"[ \t]+", " ", text)
        text = text.replace("\n", "")
        if text:
            out.append((s["title"], text))
    return out


def synth(text, voice, speed, retries=4):
    payload = json.dumps({
        "input": text,
        "voice": voice,
        "speed": float(speed),
        "pitch": 0,
        "style": "general",
    }).encode("utf-8")

    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(API, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Origin": REFERER.rstrip("/"),
            "Referer": REFERER,
            "User-Agent": UA,
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
            if data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"ID"):
                return data
            last = f"返回内容不是 MP3（前 60 字节: {data[:60]!r}）"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code} {exc.reason}"
        except Exception as exc:
            last = str(exc)
        print(f"      ! 第 {attempt}/{retries} 次失败: {last}")
        if attempt < retries:
            time.sleep(3 * attempt)
    raise RuntimeError(f"合成失败: {last}")


def duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=30).stdout
        return float(out.decode().strip())
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description="文案转配音")
    ap.add_argument("markdown", help="配音文案（Markdown，用 ## 分节）")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help=f"音色，默认 {DEFAULT_VOICE}")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, help=f"语速，默认 {DEFAULT_SPEED}")
    ap.add_argument("--outdir", default=None, help="输出目录，默认与文案同级的 audio/")
    args = ap.parse_args()

    md = os.path.abspath(args.markdown)
    if not os.path.exists(md):
        print(f"[错误] 找不到文案: {md}")
        return 1
    outdir = args.outdir or os.path.join(os.path.dirname(md), "audio")
    os.makedirs(outdir, exist_ok=True)

    sections = parse_sections(md)
    if not sections:
        print("[错误] 文案里没有解析到任何 `## ` 小节")
        return 1

    print("=" * 66)
    print(f" 文案   : {md}")
    print(f" 音色   : {args.voice}")
    print(f" 语速   : {args.speed}x")
    print(f" 小节数 : {len(sections)}")
    print(f" 输出   : {outdir}")
    print("=" * 66)

    parts, total = [], 0.0
    for i, (title, text) in enumerate(sections, 1):
        # 标题里常自带 "S01 " 之类的前缀，去掉避免文件名出现 S01_S01_
        clean = re.sub(r"^\s*[Ss]\d+[\s._-]*", "", title).strip() or title
        safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", clean).strip("_")[:28]
        name = f"S{i:02d}_{safe}.mp3"
        dest = os.path.join(outdir, name)
        print(f"\n[{i}/{len(sections)}] {title}  ({len(text)} 字)")
        try:
            audio = synth(text, args.voice, args.speed)
        except RuntimeError as exc:
            print(f"   ✗ {exc}")
            return 1
        with open(dest, "wb") as fh:
            fh.write(audio)
        d = duration(dest)
        total += d
        parts.append(dest)
        print(f"   ✓ {name}  {d:.2f}s  {len(audio)/1024:.0f} KB")

    # 合并成完整版（各段之间留 0.35s 静音，剪辑时更好切）
    full = os.path.join(outdir, "完整配音.mp3")
    lst = os.path.join(outdir, "_concat.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for p in parts:
            fh.write(f"file '{p.replace(chr(92), '/')}'\n")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-c", "copy", full], check=True, timeout=120)
        print(f"\n[合并] 完整配音.mp3  {duration(full):.2f}s")
    except Exception as exc:
        print(f"\n[合并失败] {exc}")
    finally:
        if os.path.exists(lst):
            os.remove(lst)

    print(f"\n全部完成，共 {total:.1f}s（约 {total/60:.1f} 分钟）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
