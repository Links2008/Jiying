# -*- coding: utf-8 -*-
"""即影桌面壳：内置代理服务器 + 无边框 + DWM 亚克力面板（整窗一块玻璃）。

打包：pyinstaller --onefile --noconsole --name Jiying --icon jiying.ico --add-data "index.html;." app.py
说明：
- 窗口整面就是一块亚克力：透出窗口后方的实时桌面，随位置与桌面内容实时变化；
- 系统级圆角（DWMWA_WINDOW_CORNER_PREFERENCE），页面内容铺满窗口，只有一套圆角；
- 顶栏自绘拖拽区与最小化/最大化/关闭按钮。
"""
import ctypes
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def app_root():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def pick_port(preferred=8642):
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1', preferred)) != 0:
            return preferred
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


WINDOW_TITLE = '即影 Jiying'

# 任务栏归属：显式 AppUserModelID，图标与分组落到「即影」而非 Python 默认；
# 须在窗口创建之前设置才对任务栏生效。
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Jiying.Desktop')
except Exception:
    pass


def acquire_single_instance(name='JiyingDesktopShellMutex'):
    """单实例守卫：同名互斥体已存在时说明已有实例在运行。
    返回互斥体句柄（需保持引用存活直到进程退出）；已有实例时激活旧窗口并返回 None。"""
    try:
        kernel32 = ctypes.windll.kernel32
        # 64 位下句柄是指针宽度：不声明 restype/argtypes 会被截断成 32 位，
        # 令 MutexW 返回值与 FindWindowW 拿到的窗口句柄失真。
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                          ctypes.c_wchar_p]
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, name)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            try:
                # 旧实例的窗口句柄可能稍晚才出现（冷启动中），重试几次再放弃
                for _ in range(10):
                    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
                    if hwnd:
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.SetForegroundWindow(hwnd)
                        break
                    time.sleep(0.3)
                else:
                    log('未找到已有实例窗口，无法激活')
            except Exception as e:
                log('激活已有窗口失败: ' + repr(e))
            return None
        return handle
    except Exception as e:
        log('单实例守卫不可用: ' + repr(e))
        return -1  # 守卫自身失败时不阻断启动


def set_app_user_model_id(app_id='Jiying.Desktop.Shell'):
    """任务栏按 app_id 归组：图标不退回 python 默认图标，点击才正确收起/还原。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception as e:
        log('AppUserModelID 设置失败: ' + repr(e))
        return False


LOG_FILE = os.path.join(tempfile.gettempdir(), 'jiying', 'shell.log')


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S ') + str(msg) + '\n')
    except OSError:
        pass


# ---------- DWM ----------
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
WCA_ACCENT_POLICY = 19
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

# 分层窗口（Layered Window）：WinForms 的 Form.Opacity < 1.0 底层就是这套
# API，也是兜底时唯一能绕开托管动画、直接把窗口强制成全不透明的手段。
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [('AccentState', ctypes.c_int),
                ('AccentFlags', ctypes.c_int),
                ('GradientColor', ctypes.c_uint),
                ('AnimationId', ctypes.c_int)]


class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [('Attribute', ctypes.c_int),
                ('Data', ctypes.POINTER(ACCENT_POLICY)),
                ('SizeOfData', ctypes.c_size_t)]


WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def find_hwnd_by_pid(pid=None):
    """枚举顶层窗口，找当前进程可见的主窗口"""
    user32 = ctypes.windll.user32
    target = pid or os.getpid()
    found = []

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        proc = ctypes.c_uint()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc))
        if proc.value == target and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return 0
        return 1

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def set_layered_alpha(hwnd, alpha):
    """把分层窗口的 alpha 直接写成给定值（0~255），全走 Win32、不碰 UI 线程。

    Form.Opacity 与任何 WinForms Timer 都要排在 UI 线程的消息循环里；冷启动时
    WebView2 初始化会长时间独占该线程，动画与「Invoke 兜底」会一起卡死，窗口就
    永久冻在半透明态。SetLayeredWindowAttributes 不需要消息循环，实测设进去后
    稳定保持。非分层窗口本就等同全不透明，直接放行。
    """
    try:
        user32 = ctypes.windll.user32
        h = ctypes.c_void_p(hwnd)
        style = user32.GetWindowLongW(h, GWL_EXSTYLE)
        if not (style & WS_EX_LAYERED):
            return True
        return bool(user32.SetLayeredWindowAttributes(h, 0, int(alpha), LWA_ALPHA))
    except Exception as e:
        log('分层 alpha 设置失败: ' + repr(e))
        return False


def find_main_hwnd(pid=None):
    """纯 Win32 找主窗体：本进程内标题等于 WINDOW_TITLE 的顶层窗口。

    不依赖 WinForms 对象，可从任意线程安全调用 —— 冷启动期 UI 线程被 WebView2
    占满时，native.Handle 那条路已不可用，只能靠枚举窗口拿到真正的窗体句柄。
    """
    user32 = ctypes.windll.user32
    target = pid or os.getpid()
    found = []

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        h = ctypes.c_void_p(hwnd)
        proc = ctypes.c_uint()
        user32.GetWindowThreadProcessId(h, ctypes.byref(proc))
        if proc.value != target:
            return 1
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        if buf.value == WINDOW_TITLE:
            found.append(hwnd)
            return 0
        return 1

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def enable_acrylic(hwnd):
    user32 = ctypes.windll.user32
    accent = ACCENT_POLICY()
    accent.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND
    accent.AccentFlags = 2
    # 深烟玉雾底 #0d1013 @62%：雾色由应用指定（不受系统浅色主题影响），
    # 透出桌面且保证浅色文字可读
    accent.GradientColor = 0x9E13100D  # AABBGGRR：A=0x9E B=0x13 G=0x10 R=0x0D
    data = WINDOWCOMPOSITIONATTRIBDATA()
    data.Attribute = WCA_ACCENT_POLICY
    data.Data = ctypes.pointer(accent)
    data.SizeOfData = ctypes.sizeof(accent)
    ok = bool(user32.SetWindowCompositionAttribute(ctypes.c_void_p(hwnd), ctypes.byref(data)))
    log(f'亚克力: {ok}')
    return ok


def round_corners(hwnd):
    """Win11 系统圆角（整窗唯一一套圆角）"""
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), 4)
    except Exception:
        pass


class MARGINS(ctypes.Structure):
    _fields_ = [('cxLeftWidth', ctypes.c_int),
                ('cxRightWidth', ctypes.c_int),
                ('cyTopHeight', ctypes.c_int),
                ('cyBottomHeight', ctypes.c_int)]


def extend_frame(hwnd):
    """关键一步：把玻璃延伸进客户区（-1 = 整窗都是玻璃板）。
    没有 this，亚克力/背景模糊没有任何可渲染的表面。"""
    m = MARGINS(-1, -1, -1, -1)
    r = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd), ctypes.byref(m))
    log(f'玻璃延伸进客户区: {r == 0}')
    return r == 0


def use_immersive_dark(hwnd, dark=True):
    try:
        v = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), 20, ctypes.byref(v), 4)
    except Exception:
        pass


def system_backdrop_acrylic(hwnd):
    """Win11 22H2+ 官方亚克力（DWMSBT_TRANSIENTACRYLIC），系统级模糊、无拖影"""
    backdrop = ctypes.c_int(3)  # DWMSBT_TRANSIENTACRYLIC
    r = ctypes.windll.dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), 38, ctypes.byref(backdrop), 4)
    log(f'系统背景板(官方亚克力): {r == 0}')
    return r == 0


def darken_form(native):
    """兜底：亚克力不可用时窗体底色为深色"""
    try:
        from System.Drawing import Color
        native.BackColor = Color.FromArgb(16, 20, 24)
    except Exception as e:
        log('darken_form: ' + str(e))


def apply_icon(hwnd):
    """把「即影」标记应用到窗口：任务栏 / Alt-Tab / 窗口图标与 exe 一致。

    纯 Win32 实现（不依赖 System.Drawing —— 打包环境常因 .NET 程序集
    缺失而静默失败）：LoadImageW 从 ico 文件加载大/小两档句柄，
    WM_SETICON 分别设置；图标源是随包分发的 jiying.ico（与 exe 同一份）。
    """
    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.c_uint, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_uint]
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_void_p, ctypes.c_void_p]
        ico = os.path.join(app_root(), 'jiying.ico')
        if os.path.exists(ico):
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
            for which, size in ((1, 64), (0, 32)):   # ICON_BIG / ICON_SMALL
                h = user32.LoadImageW(None, ico, IMAGE_ICON, size, size,
                                      LR_LOADFROMFILE)
                if h:
                    user32.SendMessageW(ctypes.c_void_p(hwnd), WM_SETICON,
                                        ctypes.c_void_p(which),
                                        ctypes.c_void_p(h))
            log('窗口图标: 已应用 即影标记（Win32）')
        else:
            log('窗口图标: 未找到 jiying.ico')
    except Exception as e:
        log('窗口图标应用失败: ' + repr(e))


# ---------- 窗口控制 API ----------
class Api:
    def __init__(self):
        self._win = None
        self.maxed = False
        self.acrylic_ok = False
        self._revealed = False
        # 尺寸跳变垫底的共享状态：{'timer': 兜底还原定时器, 'wv': WebView2 控件
        # 缓存, 'pad': 同步垫底委托引用}。on_shown/on_resized/resize_settled 共用。
        self.resize_fuse = {'timer': None, 'wv': None, 'pad': None}

    def is_acrylic(self):
        return self.acrylic_ok

    def minimize(self):
        if self._win:
            self._pad_before_state_change()
            self._win.minimize()

    def maximize(self):
        if not self._win:
            return
        self._pad_before_state_change()
        if self.maxed:
            self._win.restore()
            self.maxed = False
        else:
            self._win.maximize()
            self.maxed = True

    def win_state(self):
        """回传当前是否处于最大化，供前端同步「最大化 / 还原」按钮字形。

        只读、无副作用；前端取不到时保持上一次字形，按钮功能不受影响。
        走 JS→Python 查询方向（而非壳层主动推 JS），避开 UI 线程阻塞风险。
        """
        return bool(self.maxed)

    def _find_webview(self):
        """缓存查找本窗体内的 WebView2 控件（找一次，后续复用）。"""
        wv = self.resize_fuse.get('wv')
        if wv is None and self._win is not None:
            native = getattr(self._win, 'native', None)
            if native is not None:
                for c in native.Controls:
                    if type(c).__name__ == 'WebView2':
                        wv = self.resize_fuse['wv'] = c
                        break
        return wv

    def _set_pad(self, opaque):
        """垫底开关：True=WebView2 背景垫成亚克力雾色 #0d1013（盖住尺寸跳变时
        "新帧未就绪"的白色缺口帧），False=还原透明（恢复桌面模糊透出）。
        只允许在 UI 线程调用（同步垫底与 BeginInvoke 编组后的回调都在 UI 线程）。"""
        try:
            wv = self._find_webview()
            if wv is None:
                return
            from System.Drawing import Color
            wv.DefaultBackgroundColor = (
                Color.FromArgb(255, 13, 16, 19) if opaque
                else Color.Transparent)
            log('垫底=%s perf=%.3f' % (opaque, time.perf_counter()))
        except Exception as e:
            log('垫底切换失败: ' + repr(e))

    def _pad_before_state_change(self):
        """软件内按钮触发的最小化/最大化/还原：切换前先垫底，连
        DefaultBackgroundColor 属性传播的 1~2 帧也盖住。js_api 调用跑在
        pywebview 分发线程，须 BeginInvoke 编组回 UI 线程再动控件（排队先于
        pywebview 内部 maximize/minimize 的 Invoke，顺序安全）。"""
        native = getattr(self._win, 'native', None) if self._win else None
        if native is None:
            return
        try:
            from System import Action
            if native.InvokeRequired:
                native.BeginInvoke(Action(lambda: self._set_pad(True)))
            else:
                self._set_pad(True)
        except Exception as e:
            log('按钮预垫底失败: ' + repr(e))

    def resize_settled(self):
        """页面已在新尺寸下画出首帧（index.html resize 后双 rAF 调用）：
        撤掉尺寸跳变垫的深底、恢复透明亚克力，并取消兜底超时定时器。
        这是垫底撤离的事件驱动依据 —— 不再猜"多少毫秒后还原"（旧方案固定
        160ms，早于新帧真实到达时间 ~500ms，白色二次暴露即"频闪"根源）。"""
        t = self.resize_fuse.get('timer')
        if t:
            t.cancel()
            self.resize_fuse['timer'] = None
        native = getattr(self._win, 'native', None) if self._win else None
        if native is None:
            return
        try:
            from System import Action
            if native.InvokeRequired:
                native.BeginInvoke(Action(lambda: self._set_pad(False)))
            else:
                self._set_pad(False)
        except Exception as e:
            log('resize_settled 编组失败: ' + repr(e))

    def close(self):
        if self._win:
            self._win.destroy()
            # 保险丝：确保进程随窗口关闭而退出，不留僵尸端口
            threading.Timer(1.5, lambda: os._exit(0)).start()

    def app_ready(self):
        """页面首帧就绪的握手（index.html 的 reportReady 调用）：
        把预置为全透明的窗体一次性揭示（无渐变动画）—— 只揭示一次，只揭示最终版式。"""
        self._reveal()

    def _reveal(self):
        """把窗体不透明度从 0 平滑升到 1；重复调用无副作用。

        关键顺序：只有真正拿到窗体句柄(native)之后才落 _revealed 标记。
        冷启动时 WebView2 初始化可能远慢于兜底看门狗，若在 native 尚为 None 时
        就置位，页面随后的 app_ready() 握手会被 `if self._revealed: return`
        拦下，窗口就永久停在 on_before_show 设下的 Opacity=0 隐形态。
        """
        if self._revealed:
            return
        native = getattr(self._win, 'native', None) if self._win else None
        if native is None:
            return  # 窗体未就绪：不落标记，交给看门狗稍后重试
        self._revealed = True
        if not getattr(self, '_reveal_safety_started', False):
            self._reveal_safety_started = True
            threading.Thread(target=self._fade_in, daemon=True).start()

    def _fade_in(self):
        """后台线程一次性揭示（启动动画已按需求移除）：整条链路只走 Win32，
        完全不碰 WinForms 的消息循环。

        铁律：可见性绝不依赖动画。app_ready 握手已保证页面首帧就绪，这里拿到
        句柄后直接把 alpha 钉到 255 —— 揭示即终态，无渐变过程。无论中途发生
        什么，最后一定把 alpha 钉到 255，窗口必然可用。
        """
        hwnd = 0
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not hwnd:
            hwnd = find_main_hwnd() or find_hwnd_by_pid() or 0
            if not hwnd:
                time.sleep(0.05)
        if not hwnd:
            log('揭示：未找到窗口句柄，跳过')
            return
        try:
            set_layered_alpha(hwnd, 255)
        except Exception as e:
            log('揭示中断: ' + repr(e))
        # 终态不可协商：揭示是否顺利，窗口都必须全不透明。
        for _ in range(12):
            if set_layered_alpha(hwnd, 255):
                break
            time.sleep(0.2)
        # 让 WinForms 侧的 Opacity 也归位。必须用非阻塞的 BeginInvoke：阻塞式
        # Invoke 会把本线程连同 GIL 一起挂起去等 UI 线程，而冷启动期 WebView2
        # 常常正独占 UI 线程的消息循环 —— 一旦撞上就是跨运行时死锁，整个进程
        # （HTTP 服务、引擎看护线程）一起冻结，且 CPU 为 0。BeginInvoke 只入队，
        # 排不到也无所谓：OS 层的 alpha 早已钉死，可见性不受影响。
        try:
            from System import Action
            native = getattr(self._win, 'native', None)
            if native is not None:

                def _pin():
                    native.Opacity = 1.0

                action = Action(_pin)
                if native.InvokeRequired:
                    native.BeginInvoke(action)
                else:
                    _pin()
        except Exception:
            pass


# ---------- 引擎探测与自拉起 ----------
# 上架形态：用户机器上没有 Python/源码，引擎（JiyingEngine.exe）必须随包
# 分发并由壳自动拉起；开发模式则直接用解释器起源码引擎。


def engine_alive() -> int:
    """扫描 8765~8784，返回真引擎端口（/api/v1/config 200 且形状正确），无则 0。"""
    for port in range(8765, 8785):
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=0.8)
            conn.request('GET', '/api/v1/config')
            resp = conn.getresponse()
            data = resp.read().decode('utf-8', 'ignore')
            conn.close()
            if resp.status == 200 and '"download"' in data:
                return port
        except OSError:
            continue
    return 0


def launch_engine() -> bool:
    """拉起本地引擎；成功启动的判断交给调用方轮询 engine_alive()。"""
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    if getattr(sys, 'frozen', False):
        engine_exe = Path(sys.executable).parent / 'engine' / 'JiyingEngine.exe'
        if not engine_exe.exists():
            log(f'未找到自带引擎: {engine_exe}')
            return False
        subprocess.Popen([str(engine_exe)], cwd=str(engine_exe.parent), creationflags=flags)
        log('已拉起自带引擎')
        return True
    # 开发模式：用当前解释器跑源码引擎（目录与 mistdown 平级）
    dev = Path(__file__).resolve().parent.parent / 'video-subtitler'
    if (dev / 'app' / 'api' / 'server.py').exists():
        subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'app.api.server'],
                         cwd=str(dev), creationflags=flags)
        log('已拉起开发模式引擎')
        return True
    log('开发模式未找到引擎源码目录，跳过拉起')
    return False


def wait_engine(timeout: float = 30.0) -> int:
    """等引擎就绪并返回端口（超时返回 0）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = engine_alive()
        if port:
            return port
        time.sleep(0.5)
    return 0


def main():
    _mutex = acquire_single_instance()
    if _mutex is None:
        log('已有实例在运行，本次启动退出')
        return
    set_app_user_model_id()

    import server as server_mod
    port = pick_port(8642)
    srv = server_mod.create_server('127.0.0.1', port, root=app_root())
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def engine_keeper():
        # 上架机器首次冷启动：引擎不存在则拉起并等待；之后每 10s 巡检，
        # 引擎意外退出时重新拉起（限流 30s，防止引擎启动即崩时狂拉进程）
        last_spawn = 0.0
        if not engine_alive() and launch_engine():
            found = wait_engine(30)
            if found:
                server_mod.UP_PORT = found
        while True:
            time.sleep(10)
            try:
                if engine_alive():
                    server_mod.find_engine()
                elif time.monotonic() - last_spawn > 30:
                    last_spawn = time.monotonic()
                    if launch_engine():
                        found = wait_engine(30)
                        if found:
                            server_mod.UP_PORT = found
            except Exception:
                pass

    threading.Thread(target=engine_keeper, daemon=True).start()

    import webview
    # 只有"直接点中拖拽热区本身"才开始拖窗口（祖先回溯关闭）：
    # 否则点侧栏按钮、进度条、输入框时稍微挪一下鼠标，窗口就被拖走了。
    webview.settings['DRAG_REGION_DIRECT_TARGET_ONLY'] = True
    api = Api()
    win = webview.create_window(
        WINDOW_TITLE,
        f'http://127.0.0.1:{port}/',
        js_api=api,
        width=1160, height=720, min_size=(860, 560),
        frameless=True,
        easy_drag=False,
        transparent=True,        # 网页透明，亚克力从窗口底层透出
        background_color='#0b0f14',  # 深色启动底色：避免亮闪（页面背景本身全透明）
    )
    api._win = win

    # 显示前就把窗体底色清成透明：transparent 模式下 pywebview 只把 WebView2
    # 背景设为透明，却不会设置窗体自身底色，会残留 WinForms 默认的 #F0F0F0。
    # 窗口露面那一瞬就是一片白，观感就是很突然地闪出来。
    def on_before_show(window):
        try:
            from System.Drawing import Color
            window.native.BackColor = Color.Transparent
            # 先全透明：pywebview 在 transparent 模式下"导航开始即露面"，
            # 此时页面尚未绘制，直接显示就是那一下生硬白闪。
            window.native.Opacity = 0.0
            log('显示前: 底色透明、不透明度归零')
        except Exception as e:
            log('before_show 设置底色失败: ' + repr(e))

    win.events.before_show += on_before_show

    # WebView2 透明模式在窗口尺寸跳变（最大化/还原/最小化/拖边缘）时，Chromium
    # 新帧未就绪的间隙帧会整面露白（微软已知行为：透明模式不渲染背景层，尺寸
    # 跳变后新尺寸首帧要 100~500ms 才到，期间子窗口经典绘制路径露白色）。
    # 对策（两层，全事件驱动、不猜时长）：
    #  1) 同步垫底：on_shown 里挂 WinForms 原生 Form.Resize（UI 线程 WM_SIZE 内
    #     同步执行，早于子控件布局/绘制）—— 尺寸一变立即把 WebView2 控件背景
    #     垫成亚克力雾色 #0d1013，白色缺口期从第一帧起就被盖住；
    #  2) 事件驱动撤离：页面在新尺寸下真实画出首帧（index.html resize 后双
    #     rAF 调 api.resize_settled）才还原透明、恢复桌面模糊透出；下面的
    #     1.2s 定时器仅作握手丢失时的兜底（如最小化期间 rAF 暂停）。
    # 旧方案的两个缺陷即白闪根因：垫得晚（resized 事件派发到后台线程再
    # BeginInvoke 排队，缺口最初帧已白）、撤得早（固定 160ms < 新帧 ~500ms，
    # 透明背景下无帧可看，白色二次暴露形成"频闪"）。
    #
    # 本 handler 仅作同步垫底挂接失败时的备份，线程模型不变：pywebview 的
    # resized 事件 handler 跑在【后台线程】（webview/event.py 的 Event.set 无
    # 锁分支），WinForms 控件绝不能在后台线程直接读写（pythonnet 跨线程属性
    # 写会与 UI 线程产生 GIL 循环等待，实测最大化瞬间整窗死锁）。故只做两件
    # 线程安全的事：BeginInvoke 投递与 threading.Timer；控件读写全部在
    # BeginInvoke 回调（即 UI 线程）里执行。
    def on_resized(*_):
        try:
            native = getattr(win, 'native', None)
            if native is None:
                return

            def _work(opaque):
                # 本函数始终在 UI 线程执行（经 BeginInvoke 编组）
                api._set_pad(opaque)

            def _post(opaque):
                from System import Action
                if native.InvokeRequired:
                    native.BeginInvoke(Action(lambda: _work(opaque)))
                else:
                    _work(opaque)

            _post(True)

            def _restore():
                try:
                    _post(False)
                except Exception:
                    pass

            t = api.resize_fuse.get('timer')
            if t:
                t.cancel()
            api.resize_fuse['timer'] = threading.Timer(1.2, _restore)
            api.resize_fuse['timer'].start()
        except Exception as e:
            log('resize 垫底失败: ' + repr(e))

    win.events.resized += on_resized

    # 兜底：页面若因脚本异常/冷启动过慢没能握手，也必须露面，绝不留隐形窗口。
    # 用「先等 2.5s 给握手机会，再持续重试」的看门狗替代一次性定时器：
    # 一次性定时器会在 WebView2 尚未初始化完时提前触发，配合旧 _reveal 的过早
    # 置位，会把窗口永久锁死在透明态（冷启动白屏/隐形窗口的根因）。
    def reveal_watchdog():
        # 铁律：可见性绝不依赖消息循环 / 页面握手。冷启动期 WebView2 会长时间
        # 独占 UI 线程，既可能让 self._win.native 迟迟不落值（_reveal 原地返回，
        # _fade_in 从不启动），也可能让 _fade_in 末尾的 native.Opacity=1.0 迟迟
        # 不生效、随后 WinForms 按旧的 Opacity=0.0 把 alpha 写回 0。故此处一头走
        # 正常 _reveal()，一头用纯 Win32 反复把 alpha 钉到 255，直到分层样式被摘除
        # （= 真全不透明）才收工；全程不碰消息循环，动画只锦上添花。
        # 节奏：先等 2.0s 给页面握手机会，再每 0.25s 巡一遍、总窗口 20s。刻意放缓
        # 是为了避开 WebView2 初始化期 —— 早期 1.2s + 0.1s 高频轮询会在 UI 线程
        # 被独占时驱动跨线程调用，曾导致整进程卡死（CPU=0、HTTP 无响应）。
        time.sleep(2.0)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                api._reveal()
            except Exception as e:
                log('看门狗 _reveal 异常: ' + repr(e))
            hwnd = find_main_hwnd() or find_hwnd_by_pid()
            if hwnd:
                ex = ctypes.windll.user32.GetWindowLongW(
                    ctypes.c_void_p(hwnd), GWL_EXSTYLE)
                if not (ex & WS_EX_LAYERED):
                    return  # 分层样式已摘除 = 全不透明，收工
                set_layered_alpha(hwnd, 255)  # 压制 WinForms 事后写回
            time.sleep(0.25)
        log('看门狗: 20s 仍未确认全不透明')

    threading.Thread(target=reveal_watchdog, daemon=True).start()

    # 窗口态过渡（最大化 / 还原 / 最小化 / 退出）全部交棒给 Windows 原生窗口动画，
    # 前端不再自绘。这里只借这两个事件同步最大化状态，保证「最大化 / 还原」按钮在
    # Win+↑、拖到屏幕顶部等外部操作之后依然能正确切换。
    win.events.maximized += lambda: setattr(api, 'maxed', True)
    win.events.restored += lambda: setattr(api, 'maxed', False)

    # 注意：webview.start 会把窗口对象作为参数传入回调
    def on_shown(w):
        try:
            hwnd = 0
            for _ in range(20):        # 窗体句柄可能稍晚才就绪，重试最多 6 秒
                hwnd = find_hwnd_by_pid()
                if hwnd:
                    break
                time.sleep(0.3)
            if not hwnd:
                log('未找到窗口句柄')
                return
            extend_frame(hwnd)          # 关键：整窗玻璃板
            round_corners(hwnd)
            use_immersive_dark(hwnd, True)  # 原生标题栏/边框走深色
            apply_icon(hwnd)
            # 亚克力方案：优先经典 ACCENT —— 雾色由应用自定（深烟玉），
            # 不受系统浅/深色主题影响，失焦也不会塌成灰板；
            # Win11 官方系统背板(TRANSIENTACRYLIC)的雾色跟着系统主题走，
            # 浅色主题下就是一片白雾，故仅作兜底。
            if enable_acrylic(hwnd):
                api.acrylic_ok = True
            elif system_backdrop_acrylic(hwnd):
                api.acrylic_ok = True
            else:
                darken_form(getattr(w, 'native', None))
            # 窗体底色保持透明，让 DWM 玻璃透出后方桌面；
            # 不透明底色会盖死玻璃，透不出光。
            for _ in range(20):
                try:
                    from System.Drawing import Color
                    w.native.BackColor = Color.Transparent
                    log('窗体底色已确认为透明')
                    break
                except Exception:
                    time.sleep(0.3)

            # 同步垫底（白闪根治第一层）：挂 WinForms 原生 Resize 事件。pythonnet
            # 把 Python 可调用对象编组为委托，handler 在 UI 线程的 WM_SIZE 处理中
            # 同步执行 —— 早于子控件布局/绘制，尺寸跳变瞬间即垫底，不给白色缺口
            # 帧任何露面机会。Form.Resize 在最大化/还原/最小化/拖边缘都会触发；
            # 最小化瞬间垫深色，还原动画复用的缓存纹理也就是深色。
            def _sync_pad(*_):
                api._set_pad(True)

            try:
                w.native.Resize += _sync_pad
                api.resize_fuse['pad'] = _sync_pad   # 持引用防 GC
                log('同步垫底: 已挂接 Form.Resize')
            except Exception as e:
                log('同步垫底挂接失败: ' + repr(e))   # 失败则退回 resized 事件垫底
            log('on_shown 完成, acrylic=' + str(api.acrylic_ok))
        except Exception as e:
            api.acrylic_ok = False
            log('on_shown 异常: ' + repr(e))

    webview.start(on_shown, win)
    srv.shutdown()
    time.sleep(0.5)
    os._exit(0)  # 确保干净退出，防残留监听端口


if __name__ == '__main__':
    main()
