# moyu-stable-diffusion.cpp

基于 [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) 的本地 AI 生图服务，
带一套自建的中文 Web 界面，支持模型 / LoRA 切换、面部修复（ADetailer）、高清放大等。

本项目只包含**界面与部署脚本**：推理引擎（`bin/`）与模型文件（`models/`）体积过大，
不纳入版本管理，由 `scripts/setup.py` 下载或自行放置。

## 目录结构

```
.
├── webui/              # Web 界面（Python 标准库实现，无第三方依赖）
│   ├── server.py       #   HTTP 服务 + sd-server 进程管理
│   ├── index.html      #   单页前端
│   └── config.json     #   服务 / 后端 / 默认参数配置
├── scripts/
│   ├── setup.py        # 一键下载推理引擎与默认模型
│   └── convert_detectors.bat
├── model_path.json     # ★ 模型搜索目录配置（见下）
├── start.bat / .ps1 / .sh
├── bin/                # 推理引擎 + CUDA 运行时（忽略，约 1.1GB）
├── models/             # 模型文件（忽略）
├── outputs/            # 生成结果（忽略）
├── logs/               # 运行日志（忽略）
└── sd-src/             # 上游源码快照，仅作参考（忽略）
```

## 快速开始

```bash
# 1. 下载推理引擎（默认 CUDA 12 版）与一个默认模型
python scripts/setup.py

# 2. 启动服务（浏览器会自动打开）
start.bat            # 或 .\start.ps1 / python webui/server.py
```

## 模型路径配置

所有模型搜索目录统一放在**项目根目录的 `model_path.json`**：

```json
{
  "checkpoints":  ["G:/SDwebUI/models/Stable-diffusion", "models"],
  "vae":          ["G:/SDwebUI/models/VAE"],
  "text_encoder": ["G:/SDwebUI/models/text_encoder"],
  "loras":        ["G:/SDwebUI/models/Lora"],
  "embeddings":   ["G:/SDwebUI/embeddings"],
  "upscalers":    ["G:/SDwebUI/models/ESRGAN"],
  "adetailer":    ["models/adetailer"]
}
```

- 相对路径按项目根目录解析，绝对路径原样使用；沿用 A1111 / run.bat 的目录约定。
- 改完保存即可，**重启服务生效**，无需改动 `webui/config.json`。
- `webui/config.json` 中的 `paths` 仅作兜底，留空表示不覆盖 `model_path.json`。

## 说明

- 运行环境：Windows + NVIDIA GPU（CUDA 12）；界面与配置跨平台可用。
- `webui/state.json` 记录当前选中的模型，与本机路径绑定，已加入 `.gitignore`。
