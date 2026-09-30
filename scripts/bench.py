#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 webui API 跑一次生成，测量端到端耗时，并解析引擎日志的分段耗时。"""
import json, os, re, sys, time, urllib.request

BASE = "http://127.0.0.1:8080"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "sd-server.log")

PROMPT = ("1girl, solo, long hair, blue eyes, school uniform, cherry blossoms, "
          "outdoors, detailed face, best quality, masterpiece")
NEG = "lowres, bad anatomy, blurry, watermark, text, worst quality"

label = sys.argv[1] if len(sys.argv) > 1 else "run"


def api(path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


def log_size():
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


start_pos = log_size()

body = {
    "prompt": PROMPT,
    "negative_prompt": NEG,
    "width": 1024, "height": 1024,
    "sample_steps": 24, "txt_cfg": 6.0,
    "sample_method": "euler_a", "scheduler": "karras",
    "seed": 424242,
    "batch_count": 1,
}

t0 = time.time()
resp = api("/api/generate", body)
job_id = resp.get("id")
if not job_id:
    print("启动失败:", resp)
    sys.exit(1)

# 轮询直到完成
last = None
while True:
    time.sleep(1.0)
    st = api(f"/api/job?id={job_id}")
    s = st.get("status") or st.get("state")
    if s != last:
        last = s
    if s in ("done", "completed", "success", "error", "failed", "cancelled"):
        break
    if time.time() - t0 > 900:
        print("超时")
        break

elapsed = time.time() - t0

# 解析日志分段
seg = ""
try:
    with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(start_pos)
        seg = fh.read()
except OSError:
    pass


def grab(pat):
    return [float(x) for x in re.findall(pat, seg)]


cond = grab(r"get_learned_condition completed, taking ([\d.]+)s")
samp = grab(r"sampling completed, taking ([\d.]+)s")
dec = grab(r"decode_first_stage completed, taking ([\d.]+)s")
e2e = grab(r"generate_image completed in ([\d.]+)s")
steps = re.findall(r"\| (\d+)/(\d+) -", seg)

print(f"\n===== [{label}] =====")
print(f"状态        : {last}")
print(f"墙钟端到端  : {elapsed:.2f}s")
if cond: print(f"  文本编码  : {cond[-1]:.2f}s")
if samp: print(f"  采样      : {samp[-1]:.2f}s")
if dec:  print(f"  VAE 解码  : {dec[-1]:.2f}s")
if e2e:  print(f"  引擎端到端: {e2e[-1]:.2f}s")
# 采样速度：进度条里可能混有多个阶段（如主采样 + 二次细化），
# 只统计样本数最多的那个阶段，避免中位数被别的阶段带偏。
phase = {}
for num, den, val, unit in re.findall(r"\| (\d+)/(\d+) - ([\d.]+)(it/s|s/it)", seg):
    phase.setdefault(den, []).append((val, num))
if phase:
    den, items = max(phase.items(), key=lambda kv: len(kv[1]))
    vals = sorted(float(v) for v, _ in items)
    mid = vals[len(vals) // 2]
    print(f"  采样速度  : 中位 {mid:.2f}  (共 {den} 步, {len(items)} 个读数)")

errs = re.findall(r"\[ERROR[^\]]*\] ([^\n]{0,120})", seg)
for e in errs:
    # VAE 显存不足会自动回退到分块解码，属于可恢复告警
    tag = "已自动回退" if ("vae" in e.lower() or "memory" in e.lower()) else "需关注"
    print(f"  ⚠ 引擎告警: {e.strip()}  [{tag}]")
