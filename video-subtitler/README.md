# VideoSubtitler

多平台视频下载（多分辨率）+ 语音识别生成多语言字幕。粘贴链接 → 选分辨率 → 自动产出带字幕的视频。

## 功能特性

- **多平台下载**：yt-dlp 主引擎（上千站点）+ you-get / lux 备用引擎自动降级
- **多分辨率**：`--list-formats` 探测可选分辨率，按 format_id / 高度 / best 下载
- **多线程加速**：分片流并发（`-N 8`）+ aria2c 直链多连接（16 连接分段）+ 速度看门狗
- **多语言字幕**：faster-whisper 本地离线识别（99 语种、自动检测语言）→ LLM 批量翻译 → SRT
- **两种字幕形态**：软封装（秒级、不重编码）/ 硬字幕烧录（顺带多分辨率转码）
- **断点续跑**：阶段 checkpoint + 三层缓存，失败不用从头来；补翻译语言零重复开销
- **可打包分发**：PyInstaller onedir + 绿色版 zip，GitHub Actions 一键构建

## 架构（v3 蓝图）

```
入口层  CLI(py cli.py) │ HTTP 控制面(FastAPI 可选) │ (远期: pywebview 桌面壳/浏览器扩展)
编排层  PipelineRunner：阶段 checkpoint 状态机 + IO/GPU/CPU 资源池
引擎层  EngineRouter：ytdlp → youget → lux 降级链（探测缓存 24h）
能力层  transcriber(faster-whisper) / translator(LLM/null) / renderer(ffmpeg) —— 配置驱动
内核    下载内核：Task/Job 模型、协议识别、速度看门狗、.aria2 控制文件续传
存储    SQLite(task/stage/artifact/job/cache) + 文件库(library/cache/tmp)
```

## 快速开始

```bash
# 1) 安装依赖（Python ≥ 3.10）
py -m pip install -r requirements.txt
#    外部工具需 ffmpeg 在 PATH（或放入 bin/ 目录）

# 2) 看视频有哪些分辨率（两阶段交互第一步）
py cli.py "https://www.bilibili.com/video/BVxxxx" --list-formats

# 3) 下载 1080p 并生成中文字幕（源语言自动检测）
py cli.py "https://www.bilibili.com/video/BVxxxx" -r 1080p --langs zh

# 4) 多语言字幕 + 硬字幕烧录
py cli.py "https://..." -r best --langs zh,en,ja --burn

# 可选：HTTP 控制面 / 桌面应用
py -m pip install fastapi uvicorn
py -m app.api.server        # 端口自动选择（8765 起，被占用自动顺延，以日志输出为准）
py -m pip install pywebview
py main.py                  # 桌面窗口（未装 pywebview 自动回退浏览器）
```

Web UI 支持完整流程：分析分辨率 → 提交任务 → SSE 实时进度 → 产物下载 →
**字幕在线编辑 → 一键重新封装**。

首次识别会自动下载 Whisper 模型（small ≈ 460MB，存放于数据目录 models/）。

## 配置

复制 `config.example.yaml` 为 `config.yaml` 按需修改，要点：

| 配置 | 说明 |
|---|---|
| `download.aria2c.enabled` | 直链流启用 aria2c 16 连接加速（打包版建议开启） |
| `translator.provider` | `null` 只出源语言字幕；`llm` 用 OpenAI 兼容接口翻译（GLM/DeepSeek 均可） |
| `transcriber.model` | tiny/base/small/medium/large-v3，越大越准越慢 |
| `cookies` | Netscape 格式 cookies.txt，下载会员内容用 |
| `proxy` | 如 `http://127.0.0.1:7890` |

## 数据目录布局

```
<data>/                  # 绿色版=根目录/data；安装版=%LOCALAPPDATA%/VideoSubtitler
├── tasks.db             # SQLite：任务/阶段/产物/下载Job/缓存
├── library/<任务>/      # 成品：多语言 SRT + 带字幕视频
├── cache/               # 探测结果(24h) + 转写 JSON（补翻译语言时命中）
├── tmp/<任务>/          # 下载中间文件（断点续跑依赖）
└── models/ logs/
```

## 打包分发（Windows exe）

```bash
py -m pip install pyinstaller
# 把 ffmpeg.exe / ffprobe.exe / aria2c.exe / yt-dlp.exe 放入项目 bin/
pyinstaller build/build.spec --noconfirm
# dist/VideoSubtitler/ 即绿色版；推 tag 触发 .github/workflows/build.yml 自动构建发布
```

发布形态：一个 exe 三种模式（桌面 `VideoSubtitler.exe` / 服务器 `--serve` / CLI 直跑）。
更新双通道：yt-dlp 热更新（`yt-dlp.exe -U`）+ 软件自更新（查 GitHub Releases）。

## 测试

```bash
py -m pip install pytest
py -m pytest tests -q      # 全离线：FakeEngine 驱动降级链/缓存/SRT/配置
```

## 路线图

- [x] v0.1：CLI 全链路 + 引擎降级 + checkpoint + 绿色版打包
- [x] v0.2：aria2c RPC 直链加速（单一直达流）+ SSE 进度事件流 + CUDA/CPU 自动降级
- [x] v0.3：Web UI + 桌面壳（pywebview）+ 音视频双 Job 并行 + 字幕在线编辑/重渲染
- [ ] v0.4：词级时间戳（卡拉OK字幕）+ Inno Setup 安装器 + 代码签名 + 自更新

## 合规提示

⚠️ 仅用于下载和处理你有权限的内容，请遵守目标平台服务条款与当地法律。
