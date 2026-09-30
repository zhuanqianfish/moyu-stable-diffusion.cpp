#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「标注截图 + 分段配音」合成为一条可直接上传的教程视频。

约定：outputs/tutorial/NN_xxx.png 与 audio/SNN_xxx.mp3 按序号一一对应。

用法: python scripts/build_tutorial_video.py
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUT = os.path.join(ROOT, "outputs", "tutorial")
AUDIO = os.path.join(TUT, "audio")
TMP = os.path.join(TUT, "_seg")
OUT = os.path.join(TUT, "教程视频.mp4")

W, H = 1920, 1080
BG = "0x0e1014"          # 与界面深色背景一致，加边不留白
FPS = 30


def run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-800:])


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True).stdout.decode().strip()
    return float(out)


def main():
    if not os.path.isdir(AUDIO):
        print(f"[错误] 找不到配音目录: {AUDIO}")
        return 1

    shots = sorted(f for f in os.listdir(TUT)
                   if re.match(r"^\d\d_.+\.png$", f))
    audios = sorted(f for f in os.listdir(AUDIO) if f.endswith(".mp3")
                    and not f.startswith("完整"))
    if not shots or not audios:
        print("[错误] 截图或配音为空")
        return 1
    if len(shots) != len(audios):
        print(f"[警告] 截图 {len(shots)} 张，配音 {len(audios)} 段，按较少的配对")

    os.makedirs(TMP, exist_ok=True)
    n = min(len(shots), len(audios))
    segs = []
    print("=" * 62)
    for i in range(n):
        img = os.path.join(TUT, shots[i])
        aud = os.path.join(AUDIO, audios[i])
        seg = os.path.join(TMP, f"seg{i:02d}.mp4")
        # ⚠️ 光靠 -shortest 不可靠：循环图片输入的帧时间戳会提前生成，
        # 实测每个分段会多出 1.5～2 秒。显式按时长截断才准。
        dur = probe_duration(aud)
        print(f"[{i+1}/{n}] {shots[i]}  +  {audios[i]}  ({dur:.2f}s)")
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-i", img,
            "-i", aud,
            "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={BG},format=yuv420p"),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-tune", "stillimage", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-t", f"{dur:.3f}", "-shortest", seg,
        ])
        segs.append(seg)

    lst = os.path.join(TMP, "list.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for s in segs:
            fh.write(f"file '{s.replace(chr(92), '/')}'\n")
    print("\n合并中…")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", lst, "-c", "copy", OUT])

    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", OUT],
        capture_output=True).stdout.decode().strip()
    size = os.path.getsize(OUT) / 1024 / 1024
    print("=" * 62)
    print(f"完成: {OUT}")
    print(f"      {float(dur):.1f}s  ({float(dur)/60:.2f} 分钟)  {size:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
