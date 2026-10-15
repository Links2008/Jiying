# -*- coding: utf-8 -*-
"""汲影本地服务器：托管页面 + 转发 API 到 VideoSubtitler 引擎(127.0.0.1:8766)"""
import http.client
import json as _json
import os
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
UP_HOST = '127.0.0.1'
# 引擎的首选端口是 8765（见 video-subtitler 的 _pick_port(preferred=8765)）。
# 兜底值必须与之一致，否则引擎已就绪、壳却仍往 8766 打，首屏 API
# (loadConfig/refreshTasks) 会撞上 502 空窗。
UP_PORT = int(os.environ.get('JIYING_ENGINE_PORT', '8765'))
ENGINE_RANGE = range(8765, 8785)


def find_engine():
    """扫描 8765~8784：/api/v1/config 返回 200 且含 download 字段的才是真引擎。
    端口被其他服务抢占（返回别的 API 形状）时自动跳过。"""
    global UP_PORT
    for port in ENGINE_RANGE:
        try:
            conn = http.client.HTTPConnection(UP_HOST, port, timeout=1.2)
            conn.request('GET', '/api/v1/config')
            resp = conn.getresponse()
            data = resp.read().decode('utf-8', 'ignore')
            conn.close()
            if resp.status == 200 and '"download"' in data:
                if port != UP_PORT:
                    log(f'引擎端口已定位: {port}')
                    UP_PORT = port
                return port
        except OSError:
            continue
    return UP_PORT
# 桌面壳(app.py)实时捕获的当前壁纸写到这个固定位置
LIVE_WALLPAPER = os.path.join(tempfile.gettempdir(), 'jiying', 'wallpaper_live.jpg')

HOP_HEADERS = {'host', 'connection', 'accept-encoding', 'content-length', 'keep-alive', 'transfer-encoding'}
# 这些头带浏览器侧源(8642)，会被引擎的跨站校验拒绝，统一改写为引擎自身源
REWRITE_HEADERS = {'origin', 'referer', 'sec-fetch-origin', 'sec-fetch-site'}
UP_ORIGIN = 'http://127.0.0.1:8765'  # 仅文档用途；实际转发按 UP_PORT 动态计算


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    # ---- 静态文件 ----
    def _send_path(self, fp, ctype):
        """读取并发送文件；失败返回 False"""
        try:
            with open(fp, 'rb') as f:
                data = f.read()
        except OSError:
            return False
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass
        return True

    def serve_static(self):
        path = self.path.split('?')[0].split('#')[0]
        if path == '/wallpaper-live.jpg':
            # 实时桌面壁纸（桌面壳捕获），不存在则 404 由页面回退静态壁纸
            if not self._send_path(LIVE_WALLPAPER, 'image/jpeg'):
                self.send_error(404)
            return
        if path == '/wallpaper-live-ver':
            v = int(os.path.getmtime(LIVE_WALLPAPER)) if os.path.exists(LIVE_WALLPAPER) else 0
            body = _json.dumps({'v': v}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        mapping = {
            '/': ('index.html', 'text/html; charset=utf-8'),
            '/index.html': ('index.html', 'text/html; charset=utf-8'),
            '/wallpaper.jpg': ('wallpaper.jpg', 'image/jpeg'),
            '/hermes-bg.jpg': ('hermes-bg.jpg', 'image/jpeg'),
            '/etch-bg.jpg': ('etch-bg.jpg', 'image/jpeg'),
        }
        entry = mapping.get(path)
        if not entry:
            self.send_error(404)
            return
        if not self._send_path(os.path.join(ROOT, entry[0]), entry[1]):
            self.send_error(404)

    # ---- API 转发（含 SSE 流式）----
    def _send_502(self, message):
        msg = _json.dumps({'detail': {'message': message}}).encode('utf-8')
        self.send_response(502)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def proxy(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None
        headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_HEADERS or lk in REWRITE_HEADERS:
                continue
            headers[k] = v
        origin = f'http://{UP_HOST}:{UP_PORT}'   # 跟随引擎实际端口
        headers['Origin'] = origin
        headers['Referer'] = origin + '/'
        headers['Sec-Fetch-Site'] = 'same-origin'
        def _open(port):
            c = http.client.HTTPConnection(UP_HOST, port, timeout=1800)
            c.request(self.command, self.path, body=body, headers=headers)
            return c

        conn = None
        try:
            conn = _open(UP_PORT)
        except OSError:
            # 引擎可能刚换了端口：重扫后按新端口重试一次。
            # 仅在「连接/发送阶段」失败时重试 —— 此时请求尚未被引擎处理，
            # 重发不会产生重复任务（重试 POST 的安全性依赖这一点）。
            port = find_engine()
            origin = f'http://{UP_HOST}:{port}'
            headers['Origin'] = origin
            headers['Referer'] = origin + '/'
            try:
                conn = _open(port)
            except OSError:
                self._send_502('本地视频引擎未启动或无响应(8765~8784)')
                return
        try:
            resp = conn.getresponse()
        except OSError:
            # 读取响应阶段失败：请求可能已被引擎处理，绝不重试，避免重复提交
            try:
                conn.close()
            except OSError:
                pass
            self._send_502('本地视频引擎连接中断（响应读取失败）')
            return
        self.send_response(resp.status, resp.reason)
        ctype = ''
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in ('connection', 'transfer-encoding', 'content-length'):
                continue
            self.send_header(k, v)
            if lk == 'content-type':
                ctype = v
        # 流式响应（SSE / 大文件）用连接关闭定界
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            conn.close()

    def _handle(self):
        # 逐请求日志仅调试用（JIYING_DEBUG=1 开启）：常驻壳每 2.5s 轮询，
        # 全量落盘纯属磁盘负担
        if os.environ.get('JIYING_DEBUG'):
            log(f'{self.command} {self.path}')
        try:
            if self.path.startswith('/api/'):
                self.proxy()
            else:
                self.serve_static()
        except Exception as e:
            log(f'处理 {self.path} 异常: {e!r}')
            try:
                self.send_error(500, repr(e))
            except Exception:
                pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()


def log(msg):
    try:
        os.makedirs(os.path.dirname(LIVE_WALLPAPER), exist_ok=True)
        with open(os.path.join(os.path.dirname(LIVE_WALLPAPER), 'server.log'), 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S ') + str(msg) + '\n')
    except OSError:
        pass


def create_server(host='127.0.0.1', port=8642, root=None):
    """创建服务器实例；root 为静态文件目录（exe 打包时指向解包目录）"""
    global ROOT
    if root:
        ROOT = root
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == '__main__':
    srv = create_server('127.0.0.1', 8642)
    print('汲影服务器: http://127.0.0.1:8642  (引擎: %s:%d)' % (UP_HOST, UP_PORT))
    srv.serve_forever()
