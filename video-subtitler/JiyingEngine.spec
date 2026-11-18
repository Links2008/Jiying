# -*- mode: python ; coding: utf-8 -*-
# 引擎打包：py -3 -m PyInstaller JiyingEngine.spec --noconfirm
# 产物 dist/JiyingEngine/（onedir：启动快、不每次解包到临时目录；随安装包整体分发）
import os
import hashlib
import json
import lzma
from pathlib import Path
from importlib.metadata import version
from PyInstaller.config import CONF
from PyInstaller.utils.hooks import copy_metadata, collect_data_files

import faster_whisper

_version_file = Path(SPECPATH) / 'app' / 'VERSION'
_windows_version = None
if os.name == 'nt':
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    _version = _version_file.read_text(encoding='utf-8').strip()
    _parts = tuple(int(part) for part in _version.split('.'))
    if len(_parts) != 4 or any(not 0 <= part <= 65535 for part in _parts):
        raise ValueError('应用版本必须包含四个 Windows 文件版本数字。')
    _windows_version = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_parts, prodvers=_parts),
        kids=[
            StringFileInfo([StringTable('080404B0', [
                StringStruct('FileDescription', '即影本地视频引擎'),
                StringStruct('FileVersion', _version),
                StringStruct('ProductName', '即影'),
                StringStruct('ProductVersion', _version),
            ])]),
            VarFileInfo([VarStruct('Translation', [0x0804, 1200])]),
        ],
    )

if version('ctranslate2') != '4.8.2':
    raise RuntimeError('GPU 打包清单仅验证过 CTranslate2 4.8.2，请按 requirements.txt 安装依赖。')
if version('faster-whisper') != '1.2.1':
    raise RuntimeError('音频适配仅验证过 faster-whisper 1.2.1，请按 requirements.txt 安装依赖。')

# faster_whisper/assets 内是 silero VAD 的 ONNX 模型（运行期必需的数据文件，
# PyInstaller 只收代码不收数据，漏打会在转写时报 NO_SUCHFILE）
_fw_assets = os.path.join(os.path.dirname(faster_whisper.__file__), 'assets')


def _lazy_pyav_import(source):
    """仅调整固定版上游的导入时机，供冻结包使用。"""
    text = Path(source).read_text(encoding='utf-8')
    anchors = {
        '\nimport av\n': '\n',
        '    resampler = av.audio.resampler.AudioResampler(': '    import av\n\n    resampler = av.audio.resampler.AudioResampler(',
        'def _ignore_invalid_frames(frames):\n': 'def _ignore_invalid_frames(frames):\n    import av\n\n',
        'def _group_frames(frames, num_samples=None):\n': 'def _group_frames(frames, num_samples=None):\n    import av\n\n',
    }
    for original, replacement in anchors.items():
        if text.count(original) != 1:
            raise RuntimeError('固定版 Whisper 音频模块与已核验源码不符。')
        text = text.replace(original, replacement, 1)
    staged = Path(CONF['workpath']) / 'faster_whisper' / 'audio.py'
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(text, encoding='utf-8', newline='\n')
    return str(staged)


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


_src_bin = os.environ.get('JIYING_MEDIA_BIN') or os.path.join(SPECPATH, 'bin')
if os.environ.get('JIYING_MEDIA_BIN'):
    required_tools = ('ffmpeg.exe', 'ffprobe.exe', 'aria2c.exe', 'deno.exe')
    missing = [name for name in required_tools if not os.path.isfile(os.path.join(_src_bin, name))]
    if missing:
        raise RuntimeError('指定的媒体工具目录缺少：' + ', '.join(missing))
if os.path.isdir(_src_bin):
    for _f in os.listdir(_src_bin):
        _p = os.path.join(_src_bin, _f)
        if os.path.isfile(_p):
            _bin_datas.append((_p, 'bin'))
    _cuda = os.path.join(SPECPATH, 'bin', 'cuda') if os.environ.get('JIYING_BUNDLE_CUDA') == '1' else os.path.join(_src_bin, 'cuda')
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
    datas=[(_fw_assets, os.path.join('faster_whisper', 'assets')),
           (str(_version_file), 'app')] + _bin_datas
    + copy_metadata('yt-dlp') + copy_metadata('yt-dlp-ejs') + collect_data_files('yt_dlp_ejs') + [
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
        'yt_dlp_ejs', 'yt_dlp_ejs.yt.solver', 'curl_cffi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow 只用于生成 PNG 登录二维码，不需要 AVIF 编解码器。
    excludes=['tkinter', 'matplotlib', 'pandas', 'IPython', 'pytest',
              'PIL.AvifImagePlugin', 'PIL._avif', 'hf_xet', 'av'],
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
_sqlite = os.environ.get('JIYING_SQLITE_DLL')
if _sqlite:
    if not Path(_sqlite).is_file():
        raise RuntimeError('指定的 SQLite 运行库不存在。')
    a.binaries = [entry for entry in a.binaries if os.path.basename(entry[0]).lower() != 'sqlite3.dll']
    a.binaries.append(('sqlite3.dll', _sqlite, 'BINARY'))
# 新的 TOC 用于重新编译构建副本的源码，避免 PYZ 复用 Analysis 的原始字节码。
pyz = PYZ([(name, _lazy_pyav_import(source) if name == 'faster_whisper.audio' else source, kind)
           for name, source, kind in a.pure])

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='JiyingEngine',
    version=_windows_version,
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
