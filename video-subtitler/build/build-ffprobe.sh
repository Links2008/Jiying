#!/usr/bin/env bash
# 在 Windows MSYS2 UCRT64 下运行。参数为已验签的 FFmpeg 9.0.1 源码目录和独立构建目录。
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo '用法：bash build-ffprobe.sh <FFmpeg 源码目录> <构建目录>' >&2
    exit 2
fi
source_dir="$(cd "$1" && pwd -P)"
mkdir -p "$2"
cd "$2"
if [ "$(cat "$source_dir/RELEASE")" != '9.0.1' ]; then
    echo '此发布包的 ffprobe 需要 FFmpeg 9.0.1 源码。' >&2
    exit 2
fi
"$source_dir/configure" --disable-autodetect --disable-ffmpeg --disable-ffplay \
    --disable-doc --disable-debug --disable-avfilter --disable-avdevice \
    --disable-muxers --disable-encoders --disable-hwaccels --enable-schannel \
    --enable-zlib --extra-ldflags=-static
make -j "${JIYING_FFPROBE_JOBS:-8}" ffprobe.exe
./ffprobe.exe -version
