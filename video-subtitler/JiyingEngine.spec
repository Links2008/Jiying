# -*- mode: python ; coding: utf-8 -*-
# 引擎打包：py -3 -m PyInstaller JiyingEngine.spec --noconfirm
# 产物 dist/JiyingEngine/（onedir：启动快、不每次解包到临时目录；随安装包整体分发）
import os
import hashlib
import json
import lzma
from pathlib import Path
from importlib.metadata import version
from PyInstaller.utils.hooks import copy_metadata

import faster_whisper

if version('ctranslate2') != '4.8.2':
    raise RuntimeError('GPU 打包清单仅验证过 CTranslate2 4.8.2，请按 requirements.txt 安装依赖。')

# faster_whisper/assets 内是 silero VAD 的 ONNX 模型（运行期必需的数据文件，
# PyInstaller 只收代码不收数据，漏打会在转写时报 NO_SUCHFILE）
_fw_assets = os.path.join(os.path.dirname(faster_whisper.__file__), 'assets')

# 外部工具随包分发（引擎的 bin_dir() 约定 = exe 目录/bin）：
# aria2c 直链多线程下载、ffmpeg/ffprobe 音视频处理；缺失时分别降级单线程/PATH
_bin_datas = []


def _compressed_cuda(source_dir):
    """随包保留相同 GPU DLL 的无损载荷；复用时同时核对原始与压缩文件。"""
    source_dir = Path(source_dir)
    output = Path(SPECPATH) / 'build' / 'cuda-pack'
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / 'runtime.json'
    previous = json.loads(manifest.read_bytes())['files'] if manifest.is_file() else []
    records = []
    for name in ('cublas64_12.dll', 'cublasLt64_12.dll'):
        source, archive = source_dir / name, output / (name + '.xz')
        with source.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        record = {'name': name, 'size': source.stat().st_size, 'sha256': digest}
        old = next((item for item in previous if all(item.get(k) == v for k, v in record.items())), None)
        valid = False
        if old and archive.is_file():
            with archive.open('rb') as handle:
                valid = hashlib.file_digest(handle, 'sha256').hexdigest() == old.get('archive_sha256')
        if not valid:
            with source.open('rb') as handle, lzma.open(archive, 'wb', preset=9) as compressed:
                while chunk := handle.read(1024 * 1024):
                    compressed.write(chunk)
        with archive.open('rb') as handle:
            record['archive_sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
        records.append(record)
    manifest.write_text(json.dumps({'files': records}, sort_keys=True), encoding='utf-8')
    return [(str(output / (name + '.xz')), os.path.join('bin', 'cuda'))
            for name in ('cublas64_12.dll', 'cublasLt64_12.dll')] + [
        (str(manifest), os.path.join('bin', 'cuda')),
        (str(source_dir / 'NVIDIA-cublas-LICENSE.txt'), os.path.join('bin', 'cuda'))]


_src_bin = os.path.join(SPECPATH, 'bin')
if os.path.isdir(_src_bin):
    for _f in os.listdir(_src_bin):
        _p = os.path.join(_src_bin, _f)
        if os.path.isfile(_p):
            _bin_datas.append((_p, 'bin'))
    _cuda = os.path.join(_src_bin, 'cuda')
    if os.path.isdir(_cuda):
        # CTranslate2 4.8.2 使用纯 CUDA 卷积，不再依赖 cuDNN。
        if os.environ.get('JIYING_BUNDLE_CUDA') == '1':
            _bin_datas.extend(_compressed_cuda(_cuda))
        else:
            # 基础包只保留下载与校验清单；GPU 库由用户在设置中下载。
            for name in ('runtime.json', 'download.json', 'NVIDIA-cublas-LICENSE.txt'):
                source = os.path.join(_cuda, name)
                if os.path.isfile(source):
                    _bin_datas.append((source, os.path.join('bin', 'cuda')))

a = Analysis(
    ['run_engine.py'],
    pathex=[],
    binaries=[],
    datas=[(_fw_assets, os.path.join('faster_whisper', 'assets'))] + _bin_datas + copy_metadata('yt-dlp') + [
        (os.path.join(SPECPATH, 'fonts', name), 'fonts')
        for name in ('NotoSansCJKsc-Regular.otf', 'NotoSansCJKsc-OFL.txt')
    ],
    # uvicorn 运行期动态导入 loop/protocol 子模块，PyInstaller 静态分析扫不到
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on',
        'anyio._backends._asyncio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow 只用于生成 PNG 登录二维码，不需要 AVIF 编解码器。
    excludes=['tkinter', 'matplotlib', 'pandas', 'IPython', 'pytest',
              'PIL.AvifImagePlugin', 'PIL._avif'],
    noarchive=False,
    optimize=0,
)
# CUDA 组件按需下载，基础包不重复收集。Python ONNX 绑定静态链接运行库，
# 1.30.0 的独立 C API DLL 没有导入引用；真实 VAD 推理已验证可省略它。
_omit_onnx_c_api = version('onnxruntime') == '1.30.0'
a.binaries = [
    entry for entry in a.binaries
    if os.path.basename(entry[0]).lower() not in ('cublas64_12.dll', 'cublaslt64_12.dll')
    and not (_omit_onnx_c_api and entry[0].replace('\\', '/').lower() == 'onnxruntime/capi/onnxruntime.dll')
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='JiyingEngine',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # ctranslate2/onnxruntime 的 DLL 加壳后启动变慢甚至误报，不加
    console=False,      # 后台服务进程，不留黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='JiyingEngine',
)
