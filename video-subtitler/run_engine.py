# -*- coding: utf-8 -*-
"""引擎打包入口：pyinstaller JiyingEngine.spec  →  dist/JiyingEngine/

桌面壳（Jiying.exe）会在 <壳exe目录>/engine/JiyingEngine.exe 找到它并自动拉起。
windowed 模式下没有控制台可见报错，故用 faulthandler 把卡死/崩溃现场
落到 %TEMP%\\jiying\\engine_frozen_dump.log。
"""
import faulthandler
import os
import sys
import tempfile
import traceback

# windowed（console=False）模式下 std 流为 None：uvicorn 的日志格式化器
# 会调用 sys.stdout.isatty() 而崩溃（Unable to configure formatter 'default'）。
# 用 devnull 顶替，isatty() 恒为 False，行为等同无终端
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

_DUMP = os.path.join(tempfile.gettempdir(), 'jiying', 'engine_frozen_dump.log')
try:
    os.makedirs(os.path.dirname(_DUMP), exist_ok=True)
    _fh = open(_DUMP, 'w', encoding='utf-8', buffering=1)
    # 启动 20 秒后仍未完成则持续落栈（all_threads 抓全部线程）
    faulthandler.dump_traceback_later(20, repeat=True, file=_fh, all_threads=True)
except Exception:
    _fh = None

from app.api.server import main

if __name__ == '__main__':
    try:
        main()
    except Exception:
        # windowed 模式无处显示异常，落盘便于排查
        try:
            with open(_DUMP, 'a', encoding='utf-8') as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
