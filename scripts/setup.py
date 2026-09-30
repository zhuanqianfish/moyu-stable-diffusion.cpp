#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stable-diffusion.cpp 一键部署脚本
=================================

做三件事：
  1. 从 GitHub Release 下载预编译引擎（默认 CUDA 12 版，可切 CPU / Vulkan）；
  2. 解压到 bin/，CUDA 版会额外拉取 cublas / cudart 运行时；
  3. 从 ModelScope 下载一个默认可用的模型到 models/。

国内网络直连 GitHub / HuggingFace 往往不通，脚本内置了加速镜像，
失败时会自动换下一个源重试。

用法：
    python scripts/setup.py                # 引擎 + 默认模型
    python scripts/setup.py --engine cpu   # 只装 CPU 版引擎
    python scripts/setup.py --model        # 只下模型
    python scripts/setup.py --list-models  # 看可选模型
"""

import argparse
import json
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(ROOT, "bin")
MODELS_DIR = os.path.join(ROOT, "models")
CACHE_DIR = os.path.join(ROOT, ".cache")

RELEASE_TAG = "master-908-88411ef"
ASSET_PREFIX = "sd-master-88411ef-bin-win"

# GitHub 加速前缀（按顺序尝试）
GH_MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
    "",
]

# 引擎包：名称 -> (资产文件名, 是否需要 cudart)
ENGINES = {
    "cuda": ("{p}-cuda12-x64.zip", True),
    "vulkan": ("{p}-vulkan-x64.zip", False),
    "cpu": ("{p}-cpu-x64.zip", False),
    "rocm": ("{p}-rocm-7.14.0-x64.zip", False),
}
CUDART_ASSET = "cudart-sd-bin-win-cu12-x64.zip"

# 模型源（ModelScope，国内速度快）
MODELS = {
    "sd15": {
        "label": "Stable Diffusion 1.5 (推荐起步，512px，约 4.3GB)",
        "url": "https://modelscope.cn/api/v1/models/AI-ModelScope/stable-diffusion-v1-5/repo"
               "?Revision=master&FilePath=v1-5-pruned-emaonly.safetensors",
        "file": "sd-v1-5.safetensors",
    },
    "sdxl": {
        "label": "SDXL Base 1.0 (画质更好，1024px，约 6.9GB)",
        "url": "https://modelscope.cn/api/v1/models/AI-ModelScope/stable-diffusion-xl-base-1.0/repo"
               "?Revision=master&FilePath=sd_xl_base_1.0.safetensors",
        "file": "sd-xl-base-1.0.safetensors",
    },
    "sd21": {
        "label": "Stable Diffusion 2.1 (约 5.2GB)",
        "url": "https://modelscope.cn/api/v1/models/AI-ModelScope/stable-diffusion-2-1/repo"
               "?Revision=master&FilePath=v2-1_768-ema-pruned.safetensors",
        "file": "sd-v2-1.safetensors",
    },
}


def log(msg):
    print(msg, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def download(url, dest, retries=3, label=None):
    """带断点续传与进度显示的下载。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for attempt in range(1, retries + 1):
        pos = os.path.getsize(dest) if os.path.exists(dest) else 0
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (sdcpp-setup)",
            "Range": f"bytes={pos}-",
        })
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                if pos and resp.status != 206:
                    pos = 0  # 服务端不支持续传，从头来
                total = int(resp.headers.get("Content-Length") or 0) + pos
                mode = "ab" if pos else "wb"
                got = pos
                last = time.time()
                with open(dest, mode) as fh:
                    while True:
                        chunk = resp.read(1024 * 512)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if time.time() - last > 0.4:
                            pct = f"{got * 100 / total:5.1f}%" if total else "  ?  "
                            name = label or os.path.basename(dest)
                            sys.stdout.write(
                                f"\r    {name:<34} {pct}  {human(got)}"
                                + (f" / {human(total)}" if total else "")
                            )
                            sys.stdout.flush()
                            last = time.time()
            sys.stdout.write("\n")
            return True
        except Exception as exc:
            sys.stdout.write("\n")
            log(f"    ! 第 {attempt}/{retries} 次失败: {exc}")
            if attempt < retries:
                time.sleep(3)
    return False


def fetch_asset(asset_name, dest, label=None):
    """依次尝试各个 GitHub 镜像下载 Release 资产。"""
    base = (f"https://github.com/leejet/stable-diffusion.cpp/releases/download/"
            f"{RELEASE_TAG}/{asset_name}")
    for mirror in GH_MIRRORS:
        url = mirror + base
        tag = mirror or "直连"
        log(f"  源: {tag}")
        if download(url, dest, retries=2, label=label or asset_name):
            return True
        if os.path.exists(dest):
            os.remove(dest)
    return False


def extract_zip(zip_path, dest):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        z.extractall(dest)
    return names


def install_engine(engine):
    if engine not in ENGINES:
        log(f"[错误] 不支持的引擎类型: {engine}（可选: {', '.join(ENGINES)}）")
        return False

    pattern, need_cudart = ENGINES[engine]
    asset = pattern.format(p=ASSET_PREFIX)

    log(f"\n[1/3] 下载引擎包 ({engine}): {asset}")
    os.makedirs(CACHE_DIR, exist_ok=True)
    zip_path = os.path.join(CACHE_DIR, asset)
    if not os.path.exists(zip_path) or not zipfile.is_zipfile(zip_path):
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if not fetch_asset(asset, zip_path):
            log("[错误] 引擎包下载失败，请检查网络或稍后重试。")
            return False
    else:
        log("  已存在缓存，跳过下载。")

    log(f"[2/3] 解压到 bin/")
    if os.path.isdir(BIN_DIR):
        shutil.rmtree(BIN_DIR)
    names = extract_zip(zip_path, BIN_DIR)
    log(f"  解压 {len(names)} 个文件。")

    if need_cudart:
        log(f"[3/3] 下载 CUDA 运行时依赖: {CUDART_ASSET}")
        cudart_path = os.path.join(CACHE_DIR, CUDART_ASSET)
        if not os.path.exists(cudart_path) or not zipfile.is_zipfile(cudart_path):
            if os.path.exists(cudart_path):
                os.remove(cudart_path)
            if not fetch_asset(CUDART_ASSET, cudart_path):
                log("[警告] CUDA 运行时下载失败；引擎在没有它时无法使用 GPU。")
                log("       可稍后重跑: python scripts/setup.py --engine cuda")
                return True
        extract_zip(cudart_path, BIN_DIR)
        log("  CUDA 运行时已就位。")
    else:
        log("[3/3] 该引擎无需额外运行时。")

    log(f"\n[完成] 引擎已安装到: {BIN_DIR}")
    return True


def install_model(key):
    if key not in MODELS:
        log(f"[错误] 未知模型: {key}")
        return False
    info = MODELS[key]
    dest = os.path.join(MODELS_DIR, info["file"])
    os.makedirs(MODELS_DIR, exist_ok=True)

    if os.path.exists(dest):
        log(f"\n[模型] 已存在，跳过: {dest} ({human(os.path.getsize(dest))})")
        return True

    log(f"\n[模型] {info['label']}")
    log(f"  目标: {dest}")
    if not download(info["url"], dest, retries=4, label=info["file"]):
        log("[错误] 模型下载失败。")
        return False
    log(f"[完成] 模型已保存: {dest} ({human(os.path.getsize(dest))})")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="stable-diffusion.cpp 一键部署",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--engine", default="cuda", choices=list(ENGINES),
                    help="要安装的引擎类型（默认 cuda）")
    ap.add_argument("--model", default="sd15", choices=list(MODELS),
                    help="要下载的模型（默认 sd15）")
    ap.add_argument("--skip-engine", action="store_true", help="跳过引擎安装")
    ap.add_argument("--skip-model", action="store_true", help="跳过模型下载")
    ap.add_argument("--list-models", action="store_true", help="列出可选模型后退出")
    args = ap.parse_args()

    if args.list_models:
        log("可选模型：")
        for k, v in MODELS.items():
            log(f"  {k:<8} {v['label']}")
        return 0

    log("=" * 66)
    log(" stable-diffusion.cpp 部署")
    log(f" 项目目录: {ROOT}")
    log(f" 引擎: {args.engine}   模型: {args.model}")
    log("=" * 66)

    ok = True
    if not args.skip_engine:
        ok = install_engine(args.engine) and ok
    if not args.skip_model:
        ok = install_model(args.model) and ok

    log("")
    if ok:
        log("=" * 66)
        log(" 部署完成，运行以下任一命令启动服务：")
        log("   start.bat            (双击或命令行)")
        log("   .\\start.ps1          (PowerShell)")
        log("   python webui\\server.py")
        log("=" * 66)
    else:
        log("[提示] 部分步骤失败，请根据上面的日志排查后重试。")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n已取消。")
        sys.exit(130)
