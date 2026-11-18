"""Windows 当前用户的无交互 DPAPI 凭据保护。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _transform(payload: bytes, *, encrypt: bool) -> bytes:
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
    incoming, outgoing = _Blob(len(payload), buffer), _Blob()
    try:
        # 不设置 LOCAL_MACHINE，密文由当前 Windows 用户保护。禁止凭据窗口弹出。
        if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(buffer, 0, len(payload))
        if outgoing.data:
            if not encrypt:
                ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def protect(value: str) -> str:
    return base64.b64encode(_transform(value.encode("utf-8"), encrypt=True)).decode("ascii")


def unprotect(value: str) -> str:
    return _transform(base64.b64decode(value, validate=True), encrypt=False).decode("utf-8")
