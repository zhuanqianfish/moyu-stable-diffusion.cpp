#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stable-diffusion.cpp  Web 生图服务
=================================

一个零依赖（仅用 Python 标准库）的 Web 服务，职责：

  1. 托管 `sd-server`（stable-diffusion.cpp 自带的 HTTP 引擎）子进程，
     负责启动 / 健康检查 / 重启 / 切换模型；
  2. 提供生图界面（webui/index.html），单端口对外；
  3. 管理模型：大模型 / VAE / 文本编码器 / LoRA 的浏览、选择、上传、删除；
  4. 把生成的图片落盘到 outputs/ 并维护历史记录。

关于模型加载的机制（很重要）：
  - 大模型 / VAE / 文本编码器 是**引擎启动参数**，换它们必须重启 sd-server；
  - LoRA 是**按次请求**生效的（请求体里的 lora 数组），换 LoRA 不用重启，
    而且 `--lora-model-dir` 是递归扫描的，增删文件即时可见。

对外接口：
  GET  /                      生图界面
  GET  /api/status            后端与系统状态
  GET  /api/capabilities      透传 sd-server 能力表
  GET  /api/models            分类模型清单
  GET  /api/model-config      当前引擎模型配置
  POST /api/model-config      保存模型配置并重启引擎
  POST /api/backend/restart   按当前配置重启引擎
  POST /api/backend/stop      停止引擎
  GET  /api/loras             已安装 LoRA（本地扫描，含大小）
  POST /api/lora/upload       上传 LoRA
  POST /api/lora/delete       删除 LoRA
  POST /api/generate          提交生图任务
  GET  /api/job?id=           查询任务
  POST /api/cancel?id=        取消任务
  GET  /api/gallery           历史图片
  POST /api/gallery/delete    删除历史图片
  GET  /api/logs?lines=200    引擎日志
  GET  /outputs/<file>        图片文件
"""

import base64
import json
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# --------------------------------------------------------------------------- #
# 路径与配置
# --------------------------------------------------------------------------- #

WEBUI_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(WEBUI_DIR)
CONFIG_PATH = os.path.join(WEBUI_DIR, "config.json")
STATE_PATH = os.path.join(WEBUI_DIR, "state.json")

# 模型搜索目录统一放在项目根目录的 model_path.json（存在时覆盖 config.json 里的 paths）
MODEL_PATH_FILE = os.path.join(ROOT, "model_path.json")

OUTPUTS_DIR = os.path.join(ROOT, "outputs")
LOGS_DIR = os.path.join(ROOT, "logs")
HISTORY_PATH = os.path.join(OUTPUTS_DIR, "history.json")
BACKEND_LOG = os.path.join(LOGS_DIR, "sd-server.log")

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8080,
    "backend": {
        "host": "127.0.0.1",
        "port": 1234,
        "exe": "bin/sd-server.exe",
        "extra_args": [],
        "startup_timeout": 1800,
        "log_level": "info",
    },
    "paths": {
        "checkpoints": ["models"],
        "vae": [],
        "text_encoder": [],
        "loras": [],
        "embeddings": [],
        "upscalers": [],
    },
    "defaults": {
        "width": 512,
        "height": 512,
        "sample_steps": 20,
        "txt_cfg": 7.0,
        "sample_method": "euler_a",
        "scheduler": "discrete",
        "batch_count": 1,
    },
    "open_browser": True,
}

# 各类模型的扩展名
CKPT_EXTS = (".safetensors", ".ckpt", ".gguf", ".sft", ".pt", ".pth", ".bin")
LORA_EXTS = (".safetensors", ".ckpt", ".pt", ".sft")
UPSCALER_EXTS = (".pth", ".safetensors", ".pt", ".bin")

# sd-server 内置的高清修复放大算法（模型类之外的可选项）
BUILTIN_UPSCALERS = [
    "None", "Lanczos", "Nearest",
    "Latent", "Latent (nearest)", "Latent (nearest-exact)",
    "Latent (antialiased)", "Latent (bicubic)", "Latent (bicubic antialiased)",
]

# 单次上传上限（LoRA 通常几十到几百 MB）
MAX_UPLOAD = 2 * 1024 * 1024 * 1024


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass


def setup_console():
    """Windows 控制台默认可能是 GBK，先切成 UTF-8，否则中文日志会乱码/报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_model_paths():
    """从根目录 model_path.json 读取模型搜索目录；不存在则返回 None。"""
    if not os.path.exists(MODEL_PATH_FILE):
        return None
    try:
        with open(MODEL_PATH_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception as exc:
        log(f"[config] model_path.json 读取失败，改用 config.json 的 paths: {exc}")
        return None
    out = {}
    for key, val in data.items():
        if key.startswith("_"):
            continue                      # 跳过 _comment 之类的说明字段
        if isinstance(val, list):
            out[key] = val
    return out


def load_config():
    cfg = DEFAULT_CONFIG
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = _deep_merge(DEFAULT_CONFIG, json.load(fh))
        except Exception as exc:
            log(f"[config] 读取失败，使用默认配置: {exc}")
    # model_path.json 优先：只覆盖它声明了的分类，其余沿用 config.json / 默认值
    mp = load_model_paths()
    if mp:
        paths = dict(cfg.get("paths") or {})
        paths.update(mp)
        cfg["paths"] = paths
        log(f"[config] 模型路径已加载: {MODEL_PATH_FILE}")
    # 剔掉 _comment 之类的说明字段，只保留真正的目录列表
    cfg["paths"] = {k: v for k, v in (cfg.get("paths") or {}).items()
                    if not k.startswith("_") and isinstance(v, list)}
    return cfg


CFG = load_config()
BACKEND_CFG = CFG["backend"]
PATHS_CFG = CFG.get("paths", {}) or {}

for _d in (OUTPUTS_DIR, LOGS_DIR):
    os.makedirs(_d, exist_ok=True)

# sd-server 的默认批量上限是 8，超出会返回 400，这里做一次兜底钳制
BATCH_LIMIT = 8


def resolve(path):
    """把配置里的相对路径按项目根目录展开。"""
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(ROOT, path))


def norm_key(path):
    """用于跨平台比较的规范化路径键。"""
    return os.path.normcase(os.path.normpath(path)) if path else ""


# --------------------------------------------------------------------------- #
# 模型配置状态（持久化在 webui/state.json）
# --------------------------------------------------------------------------- #

STATE_DEFAULT = {
    "checkpoint": "",
    "load_mode": "auto",      # auto | checkpoint | diffusion
    "vae": "",
    "clip_l": "",
    "clip_g": "",
    "t5xxl": "",
    "llm": "",
    "lora_dir": "",
    "extra_args": [],
}

# ADetailer（面部/手部修复）。sd-server 不支持它，只能调 sd-cli 做后处理。
ADETAILER_DEFAULT = {
    "enabled": False,      # 生成完成后自动修复
    "model": "",           # 检测器（必须是从 .pt 转换来的 safetensors）
    "prompt": "",          # 留空 = 继承主提示词；支持 [PROMPT] / [SEP] / [SKIP]
    "negative_prompt": "",
    "strength": 0.4,       # 重绘幅度
    "confidence": 0.3,     # 检测置信度阈值
    "inpaint_padding": 32,
    "mask_blur": 4,
    "steps": 0,            # 0 = 继承主生成
    "cfg_scale": -1.0,     # 负数 = 继承主生成
    "max_detections": 0,   # 0 = 不限制
}

_state_lock = threading.Lock()


def load_state():
    st = dict(STATE_DEFAULT)
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                st.update(json.load(fh) or {})
        except Exception as exc:
            log(f"[state] 读取失败: {exc}")
    if not st.get("lora_dir"):
        dirs = PATHS_CFG.get("loras") or []
        st["lora_dir"] = dirs[0] if dirs else ""
    if not st.get("checkpoint"):
        # 默认优先用项目自带 models/ 里的模型（已验证可用、加载快），
        # 没有才退回到扫描到的第一个。
        local_dir = norm_key(os.path.join(ROOT, "models"))
        fallback = ""
        for c in scan_categorized().get("checkpoints", []):
            if not fallback:
                fallback = c["abs"]
            if norm_key(os.path.dirname(c["abs"])) == local_dir:
                st["checkpoint"] = c["abs"]
                break
        if not st.get("checkpoint"):
            st["checkpoint"] = fallback
    return st


def save_state(st):
    with _state_lock:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)


# STATE 需要用到下面的扫描函数，所以在扫描逻辑定义完之后再初始化，见文件下方。
STATE = dict(STATE_DEFAULT)


# --------------------------------------------------------------------------- #
# 模型扫描
# --------------------------------------------------------------------------- #

def _scan_files(dirs, exts, recursive=True, limit=4000):
    """扫描目录，返回 [{name, abs, size, dir, root, group}]，按文件名排序。

    group 是相对搜索根目录的子路径（顶层文件为 ""），
    前端据此把「多层文件夹」里的模型分组展示，而不是糊成一长条。
    """
    found = []
    seen = set()
    for d in dirs or []:
        base = resolve(d)
        if not base or not os.path.isdir(base):
            continue
        for cur, _sub, files in os.walk(base):
            for name in files:
                if not name.lower().endswith(exts):
                    continue
                full = os.path.join(cur, name)
                key = norm_key(full)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                rel = os.path.relpath(cur, base).replace("\\", "/")
                found.append({
                    "name": name,
                    "abs": os.path.normpath(full),
                    "size": size,
                    "dir": os.path.normpath(cur),
                    "root": os.path.normpath(base),
                    "group": "" if rel == "." else rel,
                })
                if len(found) >= limit:
                    return sorted(found, key=lambda x: x["name"].lower())
            if not recursive:
                break  # 只看顶层
    return sorted(found, key=lambda x: x["name"].lower())


_cat_cache = {"t": 0, "data": None}
_cat_lock = threading.Lock()


def scan_categorized(force=False):
    """扫描所有配置的模型目录，结果缓存 5 秒避免频繁磁盘遍历。"""
    with _cat_lock:
        if not force and _cat_cache["data"] and time.time() - _cat_cache["t"] < 5:
            return _cat_cache["data"]
    data = {
        "checkpoints": _scan_files(PATHS_CFG.get("checkpoints"), CKPT_EXTS),
        "vae": _scan_files(PATHS_CFG.get("vae"), CKPT_EXTS),
        "text_encoder": _scan_files(PATHS_CFG.get("text_encoder"), CKPT_EXTS),
        "embeddings": _scan_files(PATHS_CFG.get("embeddings"), (".pt", ".safetensors", ".bin")),
        # sd-server 只扫描 --hires-upscalers-dir 的顶层，子目录不认，这里保持一致
        "upscalers": _scan_files([active_upscaler_dir()], UPSCALER_EXTS, recursive=False),
    }
    with _cat_lock:
        _cat_cache["t"] = time.time()
        _cat_cache["data"] = data
    return data


def active_lora_dir():
    return resolve(STATE.get("lora_dir") or "")


def active_upscaler_dir():
    """sd-server 的 --hires-upscalers-dir 只接受一个目录，取配置里第一个存在的。"""
    for d in (PATHS_CFG.get("upscalers") or []):
        full = resolve(d)
        if full and os.path.isdir(full):
            return full
    return ""


# -- ADetailer --------------------------------------------------------------- #

def active_adetailer_dir():
    for d in (PATHS_CFG.get("adetailer") or []):
        full = resolve(d)
        if full and os.path.isdir(full):
            return full
    return ""


def scan_adetailer_models():
    """扫描可用的检测器。sd.cpp 只认 safetensors / gguf，.pt 必须转换。"""
    base = active_adetailer_dir()
    if not base:
        return []
    out = []
    for cur, _sub, files in os.walk(base):
        for name in files:
            if not name.lower().endswith((".safetensors", ".gguf")):
                continue
            full = os.path.join(cur, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            out.append({"name": os.path.splitext(name)[0], "file": name,
                        "abs": os.path.normpath(full), "size": size})
    out.sort(key=lambda x: x["name"].lower())
    return out


def scan_raw_detectors():
    """列出还没转换的 .pt 检测器，供界面提示用户转换。"""
    base = active_adetailer_dir()
    if not base:
        return []
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return []
    for name in names:
        if name.lower().endswith(".pt"):
            out.append({"name": os.path.splitext(name)[0], "file": name,
                        "abs": os.path.normpath(os.path.join(base, name))})
    out.sort(key=lambda x: x["name"].lower())
    return out


def adetailer_config():
    cfg = dict(ADETAILER_DEFAULT)
    cfg.update(STATE.get("adetailer") or {})
    return cfg


def scan_loras():
    """递归扫描当前 LoRA 目录，返回相对路径（用于请求体）与元信息。"""
    base = active_lora_dir()
    if not base or not os.path.isdir(base):
        return []
    out = []
    for cur, _sub, files in os.walk(base):
        for name in files:
            if not name.lower().endswith(LORA_EXTS):
                continue
            full = os.path.join(cur, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, base).replace("\\", "/")
            out.append({
                "name": os.path.splitext(name)[0],
                "file": name,
                "path": rel,               # 传给 sd-server 的标识
                "abs": full,
                "size": size,
                "group": os.path.dirname(rel).replace("\\", "/") or ".",
            })
    out.sort(key=lambda x: (x["group"].lower(), x["name"].lower()))
    return out


# --------------------------------------------------------------------------- #
# 模型族识别与推荐搭配
# --------------------------------------------------------------------------- #

FAMILY_RULES = [
    ("qwen-image", ("qwen-image", "qwen_image", "qwenimage")),
    ("flux", ("flux",)),
    ("sd3", ("sd3", "sd-3")),
    ("sdxl", ("xl", "illustrious", "noob", "pony", "wai", "realill",
              "chenkin", "oneobsession", "animagine", "anima")),
    ("sd15", ("v1-5", "v1_5", "sd15", "sd-1.5", "anything", "majicmix")),
    ("sd21", ("2-1", "2_1", "sd21")),
]


def detect_family(name):
    low = (name or "").lower()
    for fam, keys in FAMILY_RULES:
        for k in keys:
            if k in low:
                return fam
    return "unknown"


# 完整 checkpoint 里一定带文本编码器；只有这些前缀的键才说明它是「完整模型」
_FULL_MODEL_MARKERS = (
    "cond_stage_model.", "conditioner.", "text_encoder", "text_encoders.",
    "clip_l.", "clip_g.", "t5xxl.", "llm.", "embedder.",
)


def detect_load_mode(path):
    """判断该用 `-m`（完整模型）还是 `--diffusion-model`（纯扩散模型）。

    实测：sd.cpp 的 GGUF 量化包基本都是**纯扩散模型**，用 `-m` 会报
    "get sd version from file failed"，必须走 `--diffusion-model`
    再配合 --vae / --llm。safetensors 则读头部键名来判断。
    """
    low = (path or "").lower()
    if not low:
        return "checkpoint"
    if low.endswith(".gguf"):
        return "diffusion"
    if not low.endswith(".safetensors"):
        return "checkpoint"
    try:
        with open(path, "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            if n <= 0 or n > 64 * 1024 * 1024:
                return "checkpoint"
            hdr = json.loads(fh.read(n).decode("utf-8", "replace"))
        for key in hdr:
            if key == "__metadata__":
                continue
            if key.startswith(_FULL_MODEL_MARKERS):
                return "checkpoint"
        return "diffusion"
    except Exception:
        return "checkpoint"


def resolved_load_mode(st=None):
    """把 state 里的 load_mode（可能是 auto）解析成实际值。"""
    st = st or STATE
    mode = (st.get("load_mode") or "auto").lower()
    if mode in ("checkpoint", "diffusion"):
        return mode
    return detect_load_mode(resolve(st.get("checkpoint") or ""))


def suggest_companions(checkpoint_abs):
    """根据大模型文件名猜测需要搭配的 VAE / 文本编码器。

    返回可直接写入 state 的字段字典（只填能确定的）。
    """
    fam = detect_family(os.path.basename(checkpoint_abs or ""))
    cats = scan_categorized()
    out = {}

    def pick(cands, keys):
        for c in cands:
            low = c["name"].lower()
            if all(k in low for k in keys):
                return c["abs"]
        return ""

    if fam == "qwen-image":
        out["vae"] = pick(cats["vae"], ["qwen"]) or pick(cats["vae"], ["ae"]) 
        out["llm"] = pick(cats["text_encoder"], ["qwen"])
        out["clip_l"] = ""
        out["t5xxl"] = ""
        out["clip_g"] = ""
    elif fam == "flux":
        out["vae"] = pick(cats["vae"], ["ae"]) or pick(cats["vae"], ["flux"])
        out["clip_l"] = pick(cats["text_encoder"], ["clip_l"]) or pick(cats["text_encoder"], ["clip-l"])
        out["t5xxl"] = pick(cats["text_encoder"], ["t5"])
        out["llm"] = ""
        out["clip_g"] = ""
    elif fam == "sd3":
        out["vae"] = pick(cats["vae"], ["sd3"])
        out["clip_l"] = pick(cats["text_encoder"], ["clip_l"])
        out["clip_g"] = pick(cats["text_encoder"], ["clip_g"]) or pick(cats["text_encoder"], ["clip-g"])
        out["t5xxl"] = pick(cats["text_encoder"], ["t5"])
        out["llm"] = ""
    elif fam == "sdxl":
        out["vae"] = pick(cats["vae"], ["sdxl"]) or pick(cats["vae"], ["xl"])
        out["clip_l"] = out["clip_g"] = out["t5xxl"] = out["llm"] = ""
    else:
        # SD1.5 / SD2.x：文本编码器与 VAE 都内置在大模型里
        out["clip_l"] = out["clip_g"] = out["t5xxl"] = out["llm"] = ""
    return out


# --------------------------------------------------------------------------- #
# 后端进程管理
# --------------------------------------------------------------------------- #

# 到这里扫描函数都已定义，可以安全地载入持久化状态了
STATE.update(load_state())


class Backend:
    """管理 sd-server 子进程。"""

    def __init__(self):
        self.proc = None
        self.lock = threading.RLock()
        self.started_at = None
        self.last_error = None
        self.ready = False
        self._log_fh = None
        # 引擎输出实时打到控制台（可在 config.json 的 backend.stream_console 关掉）
        self.stream_console = bool(BACKEND_CFG.get("stream_console", True))

    @property
    def host(self):
        return BACKEND_CFG.get("host", "127.0.0.1")

    @property
    def port(self):
        return int(BACKEND_CFG.get("port", 1234))

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    # -- 命令拼装 ---------------------------------------------------------- #
    def build_cmd(self, st=None):
        st = st or STATE
        exe = resolve(BACKEND_CFG.get("exe", "bin/sd-server.exe"))
        cmd = [
            exe,
            "--listen-ip", self.host,
            "--listen-port", str(self.port),
            "--log-level", str(BACKEND_CFG.get("log_level", "info")),
        ]

        ckpt = resolve(st.get("checkpoint") or "")
        if ckpt:
            # 纯扩散模型必须用 --diffusion-model，用 -m 会加载失败
            if resolved_load_mode(st) == "diffusion":
                cmd += ["--diffusion-model", ckpt]
            else:
                cmd += ["-m", ckpt]

        # 可选的独立组件
        optional = [
            ("vae", "--vae"),
            ("clip_l", "--clip_l"),
            ("clip_g", "--clip_g"),
            ("t5xxl", "--t5xxl"),
            ("llm", "--llm"),
        ]
        for key, flag in optional:
            val = resolve(st.get(key) or "")
            if val and os.path.exists(val):
                cmd += [flag, val]

        lora_dir = active_lora_dir()
        if lora_dir and os.path.isdir(lora_dir):
            cmd += ["--lora-model-dir", lora_dir]

        emb = (PATHS_CFG.get("embeddings") or [])
        if emb:
            e0 = resolve(emb[0])
            if os.path.isdir(e0):
                cmd += ["--embd-dir", e0]

        up = active_upscaler_dir()
        if up:
            cmd += ["--hires-upscalers-dir", up]

        for a in (BACKEND_CFG.get("extra_args") or []):
            cmd.append(str(a))
        for a in (st.get("extra_args") or []):
            cmd.append(str(a))
        return cmd

    def build_cli_cmd(self, st=None):
        """给 sd-cli 用的参数：复用 build_cmd，去掉 sd-server 专属的开关。

        ADetailer 只有 CLI 支持（sd-server 完全没有这个能力），
        所以修复这一步得单独起一个 sd-cli 进程。
        """
        cmd = self.build_cmd(st)
        exe = resolve(BACKEND_CFG.get("cli_exe", "bin/sd-cli.exe"))
        out = [exe]
        drop_with_value = {"--listen-ip", "--listen-port", "--log-level"}
        drop_flag = {"--eager-load"}
        i = 1
        while i < len(cmd):
            a = cmd[i]
            if a in drop_with_value:
                i += 2
                continue
            if a in drop_flag:
                i += 1
                continue
            out.append(a)
            i += 1
        return out

    # -- 日志 -------------------------------------------------------------- #
    def _pump_output(self, pipe, log_fh):
        """把引擎输出**同时**写到日志文件和当前控制台（实时）。

        必须用 os.read() 按块读，不能用 readline()：
        sd.cpp 的进度条用 `\\r` 刷新且不换行，readline() 会一直等换行符，
        管道缓冲区（Windows 约 64KB）写满后子进程就被永久阻塞，
        表现为任务永远卡在 generating。
        """
        fd = pipe.fileno()
        try:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                if log_fh:
                    try:
                        log_fh.write(text)
                        log_fh.flush()
                    except Exception:
                        pass
                if self.stream_console:
                    try:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                    except Exception:
                        pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def tail_log(self, lines=100):
        if not os.path.exists(BACKEND_LOG):
            return []
        try:
            with open(BACKEND_LOG, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.readlines()
            return [l.rstrip("\r\n") for l in data[-lines:] if l.strip()]
        except Exception:
            return []

    # -- 启动/停止 ---------------------------------------------------------- #
    def start(self, st=None):
        with self.lock:
            self.stop_locked()
            self.cleanup_stale()

            cmd = self.build_cmd(st)
            exe = cmd[0]
            if not os.path.exists(exe):
                self.last_error = f"找不到可执行文件: {exe}"
                self.ready = False
                log(f"[backend] {self.last_error}")
                return False
            ckpt = resolve((st or STATE).get("checkpoint") or "")
            if ckpt and not os.path.exists(ckpt):
                self.last_error = f"找不到大模型文件: {ckpt}"
                self.ready = False
                log(f"[backend] {self.last_error}")
                return False

            try:
                # 行缓冲，保证子进程输出尽快落盘
                self._log_fh = open(BACKEND_LOG, "a", encoding="utf-8",
                                    errors="replace", buffering=1)
                self._log_fh.write(
                    f"\n{'=' * 70}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"启动: {' '.join(cmd)}\n{'=' * 70}\n"
                )
                self._log_fh.flush()
            except Exception:
                self._log_fh = None

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            log(f"[backend] 启动: {' '.join(cmd)}")
            try:
                # 用管道 + 独立的读取线程：一边落盘一边实时打到控制台。
                # 读取线程按块读（os.read），所以 \r 进度条不会把管道堵死。
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=os.path.dirname(exe),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except Exception as exc:
                self.last_error = f"启动失败: {exc}"
                self.proc = None
                self.ready = False
                log(f"[backend] {self.last_error}")
                return False

            threading.Thread(target=self._pump_output,
                             args=(self.proc.stdout, self._log_fh),
                             daemon=True).start()

            self.started_at = time.time()
            self.ready = False
            self.last_error = None
            return True

    def stop_locked(self):
        proc = self.proc
        self.proc = None
        self.ready = False
        if proc and proc.poll() is None:
            log("[backend] 停止旧进程…")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            except Exception as exc:
                log(f"[backend] 停止异常: {exc}")
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def stop(self):
        with self.lock:
            self.stop_locked()

    def restart(self, st=None):
        with self.lock:
            if not self.start(st):
                return False
        return self.wait_ready()

    # -- 自愈 -------------------------------------------------------------- #
    def _port_busy(self, timeout=1.0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((self.host, self.port)) == 0

    def cleanup_stale(self):
        if not self._port_busy():
            return
        if self.probe(timeout=2):
            return
        if os.name != "nt":
            return
        log("[backend] 端口被残留进程占用，正在清理 sd-server.exe …")
        try:
            subprocess.run(["taskkill", "/IM", "sd-server.exe", "/F"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            time.sleep(1.5)
        except Exception as exc:
            log(f"[backend] 清理失败: {exc}")

    # -- 就绪检测 ---------------------------------------------------------- #
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def probe(self, timeout=3):
        try:
            with urllib.request.urlopen(f"{self.base_url}/sdcpp/v1/capabilities", timeout=timeout):
                return True
        except Exception:
            return False

    def wait_ready(self, timeout=None):
        timeout = timeout or int(BACKEND_CFG.get("startup_timeout", 1800))
        deadline = time.time() + timeout
        log("[backend] 等待模型加载完成…")
        while time.time() < deadline:
            if not self.alive():
                self.last_error = self.last_error or "后端进程已退出"
                self.ready = False
                log("[backend] 进程退出，加载失败。")
                return False
            if self.probe():
                self.ready = True
                log(f"[backend] 就绪，耗时 {time.time() - (self.started_at or time.time()):.1f}s")
                return True
            time.sleep(1.5)
        self.ready = False
        self.last_error = f"等待后端就绪超时（{timeout}s）"
        log(f"[backend] {self.last_error}")
        return False

    def status(self):
        ckpt = resolve(STATE.get("checkpoint") or "")
        return {
            "alive": self.alive(),
            "ready": self.ready and self.alive(),
            "checkpoint": STATE.get("checkpoint") or "",
            "checkpoint_name": os.path.basename(ckpt) if ckpt else None,
            "checkpoint_size": os.path.getsize(ckpt) if ckpt and os.path.exists(ckpt) else None,
            "family": detect_family(os.path.basename(ckpt)) if ckpt else "unknown",
            "load_mode": resolved_load_mode(),
            "load_mode_setting": STATE.get("load_mode") or "auto",
            "pid": self.proc.pid if self.proc else None,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else None,
            "url": self.base_url,
            "last_error": self.last_error,
            "lora_dir": STATE.get("lora_dir") or "",
        }


BACKEND = Backend()


# --------------------------------------------------------------------------- #
# 与 sd-server 通信
# --------------------------------------------------------------------------- #

def backend_request(method, path, payload=None, timeout=60):
    url = BACKEND.base_url + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body.decode("utf-8")) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = {"error": {"code": "http_error",
                                "message": body.decode("utf-8", "replace")}}
        return exc.code, parsed
    except Exception as exc:
        return 0, {"error": {"code": "backend_unreachable", "message": str(exc)}}


# --------------------------------------------------------------------------- #
# ADetailer（面部/手部修复）—— 只能走 sd-cli，服务端没有这个能力
# --------------------------------------------------------------------------- #

_ad_lock = threading.Lock()


def run_adetailer(input_path, params=None, cfg=None):
    """调 `sd-cli -M adetailer` 修复一张图。

    返回 (成功?, 输出文件路径 或 None, 失败原因 或 "")。
    注意：每次都要重新加载模型，SDXL 约 45s、Qwen-Image 约 2min，
    所以这一步是「按需触发」而不是每张图都跑。
    """
    cfg = cfg or adetailer_config()
    params = params or {}

    detector = resolve(cfg.get("model") or "")
    if not detector or not os.path.exists(detector):
        return False, None, "未选择 ADetailer 检测器（.pt 需要先转换成 safetensors）"
    if not os.path.exists(input_path):
        return False, None, f"输入图片不存在: {input_path}"

    exe = resolve(BACKEND_CFG.get("cli_exe", "bin/sd-cli.exe"))
    if not os.path.exists(exe):
        return False, None, f"找不到 {exe}"

    base = os.path.splitext(os.path.basename(input_path))[0]
    out_name = f"{base}_ad{int(time.time()) % 100000}.png"
    out_path = os.path.join(OUTPUTS_DIR, out_name)

    sp = params.get("sample_params") or {}
    guidance = sp.get("guidance") or {}

    cmd = BACKEND.build_cli_cmd()
    cmd += ["-M", "adetailer", "-i", input_path, "-o", out_path,
            "--ad-model", detector,
            "--strength", str(cfg.get("strength", 0.4))]

    steps = int(sp.get("sample_steps") or 0)
    if steps:
        cmd += ["--steps", str(steps)]
    cfg_scale = guidance.get("txt_cfg")
    if cfg_scale is not None:
        cmd += ["--cfg-scale", str(cfg_scale)]
    if sp.get("sample_method"):
        cmd += ["--sampling-method", str(sp["sample_method"])]
    if sp.get("scheduler"):
        cmd += ["--scheduler", str(sp["scheduler"])]
    if params.get("prompt"):
        cmd += ["-p", str(params["prompt"])]
    if params.get("negative_prompt"):
        cmd += ["-n", str(params["negative_prompt"])]
    if cfg.get("prompt"):
        cmd += ["--ad-prompt", str(cfg["prompt"])]
    if cfg.get("negative_prompt"):
        cmd += ["--ad-negative-prompt", str(cfg["negative_prompt"])]

    extra = [
        f"confidence={cfg.get('confidence', 0.3)}",
        f"inpaint_padding={cfg.get('inpaint_padding', 32)}",
        f"mask_blur={cfg.get('mask_blur', 4)}",
    ]
    if int(cfg.get("steps") or 0) > 0:
        extra.append(f"steps={int(cfg['steps'])}")
    try:
        if float(cfg.get("cfg_scale", -1)) >= 0:
            extra.append(f"cfg_scale={float(cfg['cfg_scale'])}")
    except (TypeError, ValueError):
        pass
    if int(cfg.get("max_detections") or 0) > 0:
        extra.append(f"max_detections={int(cfg['max_detections'])}")
    cmd += ["--extra-ad-args", ",".join(extra)]

    # 同一时刻只跑一个修复任务：sd-cli 会吃满显存，并行必炸
    with _ad_lock:
        log(f"[adetailer] 开始修复 {os.path.basename(input_path)}")
        try:
            proc = subprocess.run(
                cmd, cwd=os.path.dirname(exe),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=3600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            raw = (proc.stdout or b"").decode("utf-8", "replace")
        except Exception as exc:
            return False, None, f"调用 sd-cli 失败: {exc}"

    if os.path.exists(out_path):
        log(f"[adetailer] 完成 → {out_name}")
        return True, out_path, ""

    # 失败时把有用的几行挑出来给前端看
    lines = [l.strip() for l in raw.replace("\r", "\n").split("\n") if l.strip()]
    errs = [l for l in lines if "ERROR" in l or "failed" in l.lower()]
    return False, None, "\n".join(errs[-6:]) or "sd-cli 未产出图片"


# 修复任务要跑 45 秒 ~ 2 分钟，做成后台任务让前端轮询，别把 HTTP 请求挂死
AD_TASKS = {}
AD_TASKS_LOCK = threading.Lock()


def _ad_worker(task_id, src_name, params):
    src = os.path.join(OUTPUTS_DIR, src_name)
    ok, out, err = run_adetailer(src, params)
    with AD_TASKS_LOCK:
        task = AD_TASKS.get(task_id)
        if task is None:
            return
        if not ok:
            task["status"] = "failed"
            task["error"] = err
            return
        rel = os.path.basename(out)
        try:
            size = os.path.getsize(out)
        except OSError:
            size = None
        rec = {
            "file": rel, "created": int(time.time()), "size": size,
            "seed": params.get("seed"),
            "params": dict(params, adetailer_source=src_name),
            "adetailer": True,
        }
        append_history([rec])
        task["status"] = "completed"
        task["record"] = rec


def start_adetailer_task(src_name, params):
    task_id = f"ad_{int(time.time() * 1000)}"
    with AD_TASKS_LOCK:
        # 只保留最近 30 条，避免无限增长
        for k in sorted(AD_TASKS)[:-30]:
            AD_TASKS.pop(k, None)
        AD_TASKS[task_id] = {"id": task_id, "status": "running",
                             "file": src_name, "record": None, "error": None}
    threading.Thread(target=_ad_worker, args=(task_id, src_name, params),
                     daemon=True).start()
    return task_id


# --------------------------------------------------------------------------- #
# 历史记录
# --------------------------------------------------------------------------- #

_history_lock = threading.Lock()


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(records):
    with _history_lock:
        items = records + load_history()
        items = items[:2000]
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_PATH)


def backfill_history_seeds():
    """给老记录补上 PNG 里的真实种子（幂等，只在缺字段时做）。

    sd.cpp 把参数写在 PNG 靠前的 tEXt 块里，所以只读文件头部即可，不必整张读。
    """
    items = load_history()
    changed = 0
    for it in items:
        params = it.get("params") or {}
        # 已有种子且与 params 一致就跳过（幂等）
        if it.get("seed") is not None and params.get("seed") == it.get("seed"):
            continue
        path = os.path.join(OUTPUTS_DIR, os.path.basename(it.get("file") or ""))
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                head = fh.read(512 * 1024)
            seed = png_metadata_seed(head)
            if seed is None:
                continue
            it["seed"] = seed
            # 批量生成时每张图的种子是递增的（12345 / 12346 …），
            # 所以 params 要按「这张图实际怎么来的」来存，而不是原始请求值
            params["seed"] = seed
            it["params"] = params
            changed += 1
        except Exception:
            continue
    if changed:
        with _history_lock:
            tmp = HISTORY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(items, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, HISTORY_PATH)
        log(f"[history] 已为 {changed} 条旧记录补上真实种子")


def png_metadata_seed(blob):
    """从 PNG 内嵌元数据里抠出真实种子。

    sd.cpp 默认会把 webui 风格的参数写进 PNG 的 tEXt 块（embed_image_metadata），
    其中 `Seed: 12345` 是**实际使用**的种子。请求里传 seed=-1 时，
    只有这里能查到真值 —— 不取出来的话「复用参数」就复现不出原图。
    """
    if not blob or blob[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    off, size = 8, len(blob)
    while off + 12 <= size:
        try:
            ln = struct.unpack(">I", blob[off:off + 4])[0]
            typ = blob[off + 4:off + 8]
            if typ == b"IEND":
                break
            if typ in (b"tEXt", b"iTXt") and 0 < ln < (1 << 20):
                text = blob[off + 8:off + 8 + ln].decode("utf-8", "replace")
                m = re.search(r"\bSeed:\s*(-?\d+)", text)
                if m:
                    return int(m.group(1))
        except Exception:
            return None
        off += 12 + ln
    return None


def save_images(images, params):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    records = []
    for item in images:
        idx = item.get("index", 0)
        b64 = item.get("b64_json") or ""
        if not b64:
            continue
        try:
            blob = base64.b64decode(b64)
        except Exception:
            continue

        # 优先用 PNG 里记录的真实种子，请求里的 seed 可能是 -1
        real_seed = png_metadata_seed(blob)
        req_seed = params.get("seed")
        seed_val = real_seed if real_seed is not None else req_seed
        try:
            seed_tag = str(int(seed_val)) if int(seed_val) >= 0 else "rand"
        except (TypeError, ValueError):
            seed_tag = "rand"

        name = f"{stamp}_s{seed_tag}_{idx}.png"
        with open(os.path.join(OUTPUTS_DIR, name), "wb") as fh:
            fh.write(blob)

        rec_params = dict(params)
        if real_seed is not None:
            rec_params["seed"] = real_seed      # 存真值，「复用参数」才能复现
        records.append({"file": name, "created": int(time.time()),
                        "size": len(blob), "seed": real_seed, "params": rec_params})
    if records:
        append_history(records)
    return records


# --------------------------------------------------------------------------- #
# 任务跟踪
# --------------------------------------------------------------------------- #

JOBS = {}
JOBS_LOCK = threading.Lock()


def track_job(job_id, params):
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id, "status": "queued", "created": time.time(),
            "started": None, "completed": None, "params": params,
            "images": [], "error": None, "saved": [],
        }
    threading.Thread(target=_poll_job, args=(job_id,), daemon=True).start()


def _poll_job(job_id):
    deadline = time.time() + 7200
    while time.time() < deadline:
        with JOBS_LOCK:
            if job_id not in JOBS:
                return
        code, data = backend_request("GET", f"/sdcpp/v1/jobs/{job_id}", timeout=30)
        if code == 200:
            status = data.get("status", "unknown")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job is None:
                    return
                job["status"] = status
                job["started"] = data.get("started")
                job["completed"] = data.get("completed")
                job["queue_position"] = data.get("queue_position")
                if status == "completed":
                    job["images"] = data.get("result", {}).get("images", [])
                if data.get("error"):
                    job["error"] = data["error"]
            if status == "completed":
                with JOBS_LOCK:
                    images = JOBS[job_id]["images"]
                    params = JOBS[job_id]["params"]
                records = save_images(images, params)
                with JOBS_LOCK:
                    JOBS[job_id]["saved"] = records
                    JOBS[job_id]["images"] = []
                log(f"[job] {job_id} 完成，保存 {len(records)} 张")

                # 开了「自动面部修复」就顺手跑一遍 adetailer（走 sd-cli，很慢）
                ad = adetailer_config()
                if ad.get("enabled") and ad.get("model") and records:
                    fixed = []
                    for rec in records:
                        src = os.path.join(OUTPUTS_DIR, rec["file"])
                        ok, out, err = run_adetailer(src, params, ad)
                        if not ok:
                            log(f"[adetailer] {rec['file']} 修复失败: {err}")
                            continue
                        rel = os.path.basename(out)
                        try:
                            size = os.path.getsize(out)
                        except OSError:
                            size = None
                        newrec = {"file": rel, "created": int(time.time()), "size": size,
                                  "seed": rec.get("seed"),
                                  "params": dict(params, adetailer_source=rec["file"]),
                                  "adetailer": True}
                        append_history([newrec])
                        fixed.append(newrec)
                    with JOBS_LOCK:
                        JOBS[job_id]["ad_fixed"] = fixed
                return
            if status in ("failed", "cancelled"):
                log(f"[job] {job_id} {status}")
                return
        elif code in (404, 410):
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["status"] = "failed"
                    JOBS[job_id]["error"] = data.get("error") or {"message": "任务已过期"}
            return
        time.sleep(0.7)
    with JOBS_LOCK:
        if job_id in JOBS and JOBS[job_id]["status"] not in ("completed", "failed", "cancelled"):
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = {"message": "等待超时"}


def prune_jobs():
    now = time.time()
    with JOBS_LOCK:
        for key in [k for k, v in JOBS.items() if now - v["created"] > 7200]:
            JOBS.pop(key, None)


# --------------------------------------------------------------------------- #
# 参数组装
# --------------------------------------------------------------------------- #

def build_payload(body):
    d = CFG.get("defaults", {})

    def num(key, cast, default):
        try:
            return cast(body.get(key, default))
        except (TypeError, ValueError):
            return default

    payload = {
        "prompt": str(body.get("prompt") or "").strip(),
        "negative_prompt": str(body.get("negative_prompt") or ""),
        "width": num("width", int, int(d.get("width", 512))),
        "height": num("height", int, int(d.get("height", 512))),
        "seed": num("seed", int, -1),
        "batch_count": max(1, min(BATCH_LIMIT, num("batch_count", int, 1))),
        "clip_skip": num("clip_skip", int, -1),
        "sample_params": {
            "sample_method": str(body.get("sample_method") or d.get("sample_method", "euler_a")),
            "scheduler": str(body.get("scheduler") or d.get("scheduler", "discrete")),
            "sample_steps": max(1, min(150, num("sample_steps", int, int(d.get("sample_steps", 20))))),
            "guidance": {"txt_cfg": num("txt_cfg", float, float(d.get("txt_cfg", 7.0)))},
        },
        "embed_image_metadata": True,
        "output_format": "png",
    }

    eta = body.get("eta")
    if eta not in (None, ""):
        try:
            payload["sample_params"]["eta"] = float(eta)
        except ValueError:
            pass

    # LoRA：按次请求生效，无需重启引擎
    loras = body.get("loras") or []
    lora_list = []
    for item in loras:
        if isinstance(item, dict) and item.get("path"):
            try:
                mult = float(item.get("multiplier", 1.0))
            except (TypeError, ValueError):
                mult = 1.0
            if mult == 0:
                continue
            lora_list.append({"path": item["path"], "multiplier": mult})
    if lora_list:
        payload["lora"] = lora_list

    if body.get("hires_enabled"):
        payload["hires"] = {
            "enabled": True,
            "upscaler": str(body.get("hires_upscaler") or "Latent"),
            "scale": float(body.get("hires_scale") or 2.0),
            "denoising_strength": float(body.get("hires_denoising_strength") or 0.7),
            "steps": int(body.get("hires_steps") or 0),
        }

    return payload


# --------------------------------------------------------------------------- #
# multipart 解析（仅用于 LoRA 上传）
# --------------------------------------------------------------------------- #

def parse_multipart(body, boundary):
    """返回 [(field_name, filename, data_bytes)]。"""
    delim = b"--" + boundary
    out = []
    for seg in body.split(delim):
        if not seg or seg in (b"--\r\n", b"--"):
            continue
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        head_end = seg.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        head = seg[:head_end].decode("utf-8", "replace")
        data = seg[head_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name = ""
        filename = ""
        for line in head.split("\r\n"):
            if line.lower().startswith("content-disposition"):
                m = re.search(r'name="([^"]*)"', line)
                if m:
                    name = m.group(1)
                m = re.search(r'filename="([^"]*)"', line)
                if m:
                    filename = m.group(1)
        out.append((name, filename, data))
    return out


def safe_filename(name):
    """去掉路径成分与危险字符，避免目录穿越。"""
    name = os.path.basename(name or "").strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    return name or "upload.safetensors"


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "sdcpp-webui/2.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        pass

    # -- 路由 -------------------------------------------------------------- #
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端提前断开（轮询/刷新），不必记录
        except Exception:
            log("[http] " + traceback.format_exc())
            self._safe_error()

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            log("[http] " + traceback.format_exc())
            self._safe_error()

    def _safe_error(self):
        try:
            self._send(500, {"error": {"message": "internal error"}})
        except Exception:
            pass

    def _route_get(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_file(os.path.join(WEBUI_DIR, "index.html"),
                                    "text/html; charset=utf-8")

        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        if path == "/api/status":
            return self._send(200, self._status_payload())

        if path == "/api/capabilities":
            if not BACKEND.probe():
                return self._send(503, {"error": {"message": "后端尚未就绪"}})
            code, data = backend_request("GET", "/sdcpp/v1/capabilities", timeout=30)
            return self._send(code or 502, data)

        if path == "/api/models":
            # 遍历模型目录可能很慢（尤其在网络盘/慢盘上），默认走缓存，
            # 前端点「刷新」时带 ?refresh=1 才强制重扫。
            force = (query.get("refresh") or ["0"])[0] not in ("0", "", "false")
            cats = scan_categorized(force=force)
            return self._send(200, {
                "checkpoints": cats["checkpoints"],
                "vae": cats["vae"],
                "text_encoder": cats["text_encoder"],
                "embeddings": cats["embeddings"],
                "upscalers": cats["upscalers"],
                "lora_dirs": PATHS_CFG.get("loras") or [],
                "state": STATE,
            })

        if path == "/api/model-config":
            return self._send(200, {
                "state": STATE,
                "command": BACKEND.build_cmd(),
                "suggest": suggest_companions(STATE.get("checkpoint") or ""),
            })

        if path == "/api/loras":
            return self._send(200, {
                "lora_dir": STATE.get("lora_dir") or "",
                "lora_dirs": PATHS_CFG.get("loras") or [],
                "items": scan_loras(),
            })

        if path == "/api/upscalers":
            # 引擎就绪时以它上报的列表为准（它才知道哪些真的可用），
            # 否则回退到内置清单 + 目录扫描结果。
            builtin = list(BUILTIN_UPSCALERS)
            if BACKEND.probe(timeout=2):
                code, caps = backend_request("GET", "/sdcpp/v1/capabilities", timeout=20)
                if code == 200 and caps.get("upscalers"):
                    names = [u.get("name") for u in caps["upscalers"] if u.get("name")]
                    if names:
                        builtin = names
            models = scan_categorized().get("upscalers", [])
            return self._send(200, {
                "builtin": builtin,
                "models": [{"name": os.path.splitext(m["name"])[0],
                            "file": m["name"], "size": m["size"],
                            "dir": m["dir"]} for m in models],
                "dirs": PATHS_CFG.get("upscalers") or [],
            })

        if path == "/api/detect-load-mode":
            p = (query.get("path") or [""])[0]
            full = resolve(p) if p else ""
            if not full or not os.path.isfile(full):
                return self._send(400, {"error": {"message": "文件不存在"}})
            mode = detect_load_mode(full)
            if full.lower().endswith(".gguf"):
                reason = "GGUF 量化包通常是纯扩散模型"
            elif mode == "diffusion":
                reason = "未找到内置文本编码器，按纯扩散模型加载"
            else:
                reason = "包含内置文本编码器，按完整模型加载"
            return self._send(200, {
                "mode": mode,
                "family": detect_family(os.path.basename(full)),
                "reason": reason,
            })

        if path == "/api/adetailer/models":
            return self._send(200, {
                "dir": active_adetailer_dir(),
                "dirs": PATHS_CFG.get("adetailer") or [],
                "models": scan_adetailer_models(),
                "raw": scan_raw_detectors(),
                "config": adetailer_config(),
            })

        if path == "/api/adetailer/task":
            tid = (query.get("id") or [""])[0]
            with AD_TASKS_LOCK:
                task = AD_TASKS.get(tid)
            if not task:
                return self._send(404, {"error": {"message": "未知任务"}})
            return self._send(200, task)

        if path == "/api/job":
            job_id = (query.get("id") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job:
                    return self._send(404, {"error": {"message": "未知任务"}})
                return self._send(200, {
                    "id": job["id"], "status": job["status"],
                    "created": job["created"], "started": job.get("started"),
                    "completed": job.get("completed"),
                    "queue_position": job.get("queue_position"),
                    "saved": job.get("saved", []),
                    "ad_fixed": job.get("ad_fixed", []),
                    "error": job.get("error"), "params": job.get("params"),
                })

        if path == "/api/gallery":
            items = load_history()
            return self._send(200, {"items": items, "total": len(items)})

        if path == "/api/logs":
            lines = int((query.get("lines") or ["200"])[0])
            return self._send(200, {"lines": BACKEND.tail_log(lines), "path": BACKEND_LOG})

        if path.startswith("/outputs/"):
            safe = os.path.basename(unquote(path[len("/outputs/"):]))
            full = os.path.join(OUTPUTS_DIR, safe)
            if not os.path.exists(full):
                return self._send(404, {"error": {"message": "图片不存在"}})
            return self._serve_file(full, mimetypes.guess_type(full)[0] or "application/octet-stream")

        return self._send(404, {"error": {"message": f"未知路径 {path}"}})

    def _route_post(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/api/generate":
            body = self._json_body()
            if not BACKEND.probe():
                return self._send(503, {"error": {"message": "后端尚未就绪，请稍候"}})
            payload = build_payload(body)
            if not payload["prompt"]:
                return self._send(400, {"error": {"message": "提示词不能为空"}})
            code, data = backend_request("POST", "/sdcpp/v1/img_gen", payload, timeout=120)
            if code in (200, 202) and data.get("id"):
                track_job(data["id"], payload)
                prune_jobs()
                return self._send(202, {"id": data["id"], "status": data.get("status", "queued")})
            return self._send(code or 502, data)

        if path == "/api/cancel":
            job_id = (query.get("id") or [""])[0]
            code, data = backend_request("POST", f"/sdcpp/v1/jobs/{job_id}/cancel", {}, timeout=30)
            return self._send(code or 502, data)

        if path == "/api/model-config":
            body = self._json_body()
            new_state = dict(STATE)
            for key in ("checkpoint", "load_mode", "vae", "clip_l", "clip_g", "t5xxl", "llm", "lora_dir"):
                if key in body:
                    if key == "load_mode":
                        mode = str(body.get(key) or "auto").lower()
                        new_state[key] = mode if mode in ("auto", "checkpoint", "diffusion") else "auto"
                        continue
                    # 规范化：统一分隔符，消掉 // 和 ../ 之类
                    raw = str(body.get(key) or "").strip()
                    new_state[key] = os.path.normpath(raw) if raw else ""
            if body.get("extra_args") is not None:
                new_state["extra_args"] = [str(a) for a in (body.get("extra_args") or [])]

            ckpt = resolve(new_state.get("checkpoint") or "")
            if not ckpt or not os.path.exists(ckpt):
                return self._send(400, {"error": {"message": f"大模型文件不存在: {ckpt or '(空)'}"}})
            if not os.path.isfile(ckpt):
                return self._send(400, {"error": {"message": f"这不是一个文件: {ckpt}"}})

            STATE.clear()
            STATE.update(new_state)
            save_state(STATE)
            scan_categorized(force=True)
            log(f"[state] 模型配置已更新: {os.path.basename(ckpt)}")
            threading.Thread(target=BACKEND.restart, daemon=True).start()
            return self._send(202, {"message": "配置已保存，引擎正在重启…", "state": STATE})

        if path == "/api/adetailer/config":
            body = self._json_body()
            cfg = adetailer_config()
            for k in ADETAILER_DEFAULT:
                if k in body:
                    cfg[k] = body[k]
            cfg["enabled"] = bool(cfg.get("enabled"))
            for k, dflt, cast in (("strength", 0.4, float), ("confidence", 0.3, float),
                                  ("cfg_scale", -1.0, float),
                                  ("inpaint_padding", 32, int), ("mask_blur", 4, int),
                                  ("steps", 0, int), ("max_detections", 0, int)):
                try:
                    cfg[k] = cast(cfg.get(k, dflt))
                except (TypeError, ValueError):
                    cfg[k] = dflt
            cfg["model"] = str(cfg.get("model") or "")
            cfg["prompt"] = str(cfg.get("prompt") or "")
            cfg["negative_prompt"] = str(cfg.get("negative_prompt") or "")
            STATE["adetailer"] = cfg
            save_state(STATE)
            log(f"[adetailer] 配置已保存（启用={cfg['enabled']}）")
            return self._send(200, {"message": "已保存", "adetailer": cfg})

        if path == "/api/adetailer/repair":
            body = self._json_body()
            name = os.path.basename(str(body.get("file") or ""))
            src = os.path.join(OUTPUTS_DIR, name)
            if not name or not os.path.exists(src):
                return self._send(404, {"error": {"message": "图片不存在"}})
            if not adetailer_config().get("model"):
                return self._send(400, {"error": {"message":
                    "未选择检测器。.pt 模型需要先转换成 safetensors 才能用"}})
            rec = next((x for x in load_history() if x.get("file") == name), None)
            params = (rec or {}).get("params") or {}
            task_id = start_adetailer_task(name, params)
            return self._send(202, {"id": task_id, "status": "running"})

        if path == "/api/backend/restart":
            threading.Thread(target=BACKEND.restart, daemon=True).start()
            return self._send(202, {"message": "正在重启后端…"})

        if path == "/api/backend/stop":
            BACKEND.stop()
            return self._send(200, {"message": "后端已停止"})

        if path == "/api/lora/upload":
            return self._handle_lora_upload()

        if path == "/api/lora/delete":
            body = self._json_body()
            base = active_lora_dir()
            if not base:
                return self._send(400, {"error": {"message": "未配置 LoRA 目录"}})
            removed, failed = [], []
            for rel in (body.get("paths") or []):
                target = os.path.normpath(os.path.join(base, rel))
                # 防目录穿越
                if not norm_key(target).startswith(norm_key(base) + os.sep):
                    failed.append(rel)
                    continue
                try:
                    if os.path.isfile(target):
                        os.remove(target)
                        removed.append(rel)
                    else:
                        failed.append(rel)
                except Exception as exc:
                    log(f"[lora] 删除失败 {rel}: {exc}")
                    failed.append(rel)
            log(f"[lora] 删除 {len(removed)} 个，失败 {len(failed)} 个")
            return self._send(200, {"removed": removed, "failed": failed})

        if path == "/api/gallery/delete":
            body = self._json_body()
            names = body.get("files") or []
            removed = []
            with _history_lock:
                keep = []
                for it in load_history():
                    if it.get("file") in names:
                        try:
                            t = os.path.join(OUTPUTS_DIR, os.path.basename(it["file"]))
                            if os.path.exists(t):
                                os.remove(t)
                        except Exception:
                            pass
                        removed.append(it["file"])
                    else:
                        keep.append(it)
                tmp = HISTORY_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(keep, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, HISTORY_PATH)
            return self._send(200, {"removed": removed})

        return self._send(404, {"error": {"message": f"未知路径 {path}"}})

    # -- LoRA 上传 ---------------------------------------------------------- #
    def _handle_lora_upload(self):
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype:
            return self._send(400, {"error": {"message": "需要 multipart/form-data"}})
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not m:
            return self._send(400, {"error": {"message": "缺少 boundary"}})
        boundary = (m.group(1) or m.group(2)).strip().encode()

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._send(400, {"error": {"message": "请求体为空"}})
        if length > MAX_UPLOAD:
            return self._send(413, {"error": {"message":
                f"文件过大（上限 {MAX_UPLOAD // 1024**3} GB）"}})

        body = self.rfile.read(length)
        parts = parse_multipart(body, boundary)
        if not parts:
            return self._send(400, {"error": {"message": "解析 multipart 失败"}})

        base = active_lora_dir()
        if not base:
            return self._send(400, {"error": {"message": "未配置 LoRA 目录"}})

        # 可选：把上传的 LoRA 归到某个子目录
        subdir = ""
        for field, _fn, data in parts:
            if field == "subdir":
                subdir = safe_filename(data.decode("utf-8", "replace")).strip()
                subdir = subdir if subdir not in ("", ".", "upload.safetensors") else ""
                break

        saved = []
        for field, filename, data in parts:
            if field != "files" or not filename:
                continue
            fname = safe_filename(filename)
            if not fname.lower().endswith(LORA_EXTS):
                fname += ".safetensors"
            target_dir = os.path.join(base, subdir) if subdir else base
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, fname)
            with open(target, "wb") as fh:
                fh.write(data)
            saved.append(os.path.relpath(target, base).replace("\\", "/"))
            log(f"[lora] 已保存 {target} ({len(data) / 1024 ** 2:.1f} MB)")

        if not saved:
            return self._send(400, {"error": {"message": "没有收到文件字段 files"}})
        return self._send(200, {"saved": saved})

    # -- 状态 -------------------------------------------------------------- #
    def _status_payload(self):
        st = BACKEND.status()
        st["ready"] = BACKEND.probe()
        # 默认参数按模型族微调，分辨率错了出图质量会明显变差
        d = dict(CFG.get("defaults", {}))
        d.update(FAMILY_DEFAULTS.get(st.get("family") or "unknown", {}))
        st["defaults"] = d
        st["webui_port"] = CFG.get("port", 8080)
        st["gpu"] = gpu_info()
        st["outputs_count"] = len(load_history())
        st["state"] = STATE
        st["adetailer"] = adetailer_config()
        if not st["ready"]:
            st["loading"] = backend_loading_progress()
        return st

    def _serve_file(self, path, ctype):
        if not os.path.exists(path):
            return self._send(404, {"error": {"message": "文件不存在"}})
        with open(path, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype)


_PROGRESS_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*-\s*[\d.]+\s*[KMG]?B/s")


class QuietServer(ThreadingHTTPServer):
    """客户端提前断开是常态（轮询/切页），别把它当异常刷屏。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def backend_loading_progress():
    """从日志里抓最近一条权重加载进度，形如 "269/686"。

    sd.cpp 加载 safetensors 是逐张量随机读，慢盘/冷缓存上可能要好几分钟，
    没有这个提示用户会以为卡死了。
    """
    for line in reversed(BACKEND.tail_log(80)):
        for chunk in reversed(line.replace("\r", "\n").split("\n")):
            m = _PROGRESS_RE.search(chunk)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                if total > 0:
                    return {"done": done, "total": total,
                            "percent": round(done * 100.0 / total, 1)}
    return None


# 不同模型族的合理默认参数：分辨率搞错是新手最常见的翻车点
FAMILY_DEFAULTS = {
    "sd15":       {"width": 512,  "height": 512,  "txt_cfg": 7.0, "sample_steps": 20,
                   "sample_method": "euler_a", "scheduler": "discrete"},
    "sd21":       {"width": 768,  "height": 768,  "txt_cfg": 7.5, "sample_steps": 20,
                   "sample_method": "euler_a", "scheduler": "discrete"},
    "sdxl":       {"width": 1024, "height": 1024, "txt_cfg": 6.0, "sample_steps": 24,
                   "sample_method": "euler_a", "scheduler": "karras"},
    "flux":       {"width": 1024, "height": 1024, "txt_cfg": 1.0, "sample_steps": 20,
                   "sample_method": "euler",   "scheduler": "simple"},
    "sd3":        {"width": 1024, "height": 1024, "txt_cfg": 4.5, "sample_steps": 24,
                   "sample_method": "euler",   "scheduler": "discrete"},
    "qwen-image": {"width": 1024, "height": 1024, "txt_cfg": 6.0, "sample_steps": 20,
                   "sample_method": "euler",   "scheduler": "simple"},
}


def gpu_info():
    """显卡信息。结果缓存 —— 它不会变，而前端每几秒就轮询一次状态，
    每次都去 spawn 一个 PowerShell 进程是不可接受的。"""
    global _GPU_CACHE
    with _GPU_LOCK:
        if _GPU_CACHE is not None:
            return dict(_GPU_CACHE)

    info = {"name": None, "driver": None}
    if os.name == "nt":
        try:
            ps = ("$g = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like '*NVIDIA*' } "
                  "| Select-Object -First 1; if ($g) { Write-Output ($g.Name + '|' + $g.DriverVersion) }")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = (out.stdout or "").strip()
            if "|" in text:
                name, drv = text.split("|", 1)
                info["name"] = name.strip()
                info["driver"] = drv.strip()
        except Exception:
            pass

    with _GPU_LOCK:
        _GPU_CACHE = dict(info)
    return info


_GPU_CACHE = None
_GPU_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

def open_browser_later(url):
    def _open():
        time.sleep(1.0)
        try:
            if os.name == "nt":
                os.startfile(url)  # noqa: S606
            else:
                import webbrowser
                webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main():
    setup_console()
    host = CFG.get("host", "127.0.0.1")
    port = int(CFG.get("port", 8080))
    url = f"http://{host}:{port}/"

    log("=" * 68)
    log(" stable-diffusion.cpp Web 生图服务")
    log(f" 项目目录 : {ROOT}")
    log(f" 界面地址 : {url}")
    log(f" 大模型   : {os.path.basename(resolve(STATE.get('checkpoint') or '')) or '(未设置)'}")
    log(f" LoRA 目录: {STATE.get('lora_dir') or '(未设置)'}")
    log("=" * 68)

    # 预热：显卡信息与模型列表都很慢，放到后台线程先算好，
    # 避免前端第一次轮询时卡住。
    threading.Thread(target=lambda: (gpu_info(), scan_categorized()), daemon=True).start()
    threading.Thread(target=backfill_history_seeds, daemon=True).start()
    threading.Thread(target=lambda: BACKEND.start() and BACKEND.wait_ready(),
                     daemon=True).start()

    httpd = QuietServer((host, port), Handler)
    if CFG.get("open_browser", True):
        open_browser_later(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，正在关闭…")
    finally:
        BACKEND.stop()
        httpd.server_close()
        log("已退出。")


if __name__ == "__main__":
    main()
