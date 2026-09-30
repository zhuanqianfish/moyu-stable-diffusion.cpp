#!/usr/bin/env bash
# stable-diffusion.cpp Web 生图服务 —— 启动脚本 (Git Bash / Linux / macOS)
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

echo
echo "=============================================================="
echo "   stable-diffusion.cpp  Web 生图服务"
echo "--------------------------------------------------------------"
echo "   项目目录 : $ROOT"
echo "=============================================================="
echo

# ---------- 1. 检查引擎 ----------
if [ ! -f "$ROOT/bin/sd-server.exe" ] && [ ! -f "$ROOT/bin/sd-server" ]; then
  echo "[错误] 未找到推理引擎 (bin/sd-server.exe)"
  echo "       请先执行: python scripts/setup.py"
  exit 1
fi

# ---------- 2. 模型路径来自 model_path.json（不再检查本地 models/ 目录） ----------
if [ -f "$ROOT/model_path.json" ]; then
  echo "[信息] 模型路径配置: model_path.json"
else
  echo "[提示] 未找到 model_path.json，将回退到 webui/config.json 的 paths"
fi

# ---------- 3. 查找 Python ----------
PY=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  for cand in \
    "$HOME/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe" \
    "$HOME/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3" \
    "/usr/bin/python3"
  do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi

if [ -z "$PY" ]; then
  echo "[错误] 未检测到 Python 3.8+，请先安装。"
  exit 1
fi

echo "[信息] 使用 Python: $PY"
echo "[信息] 正在启动服务，浏览器会自动打开。按 Ctrl+C 停止。"
echo

exec "$PY" "$ROOT/webui/server.py"
