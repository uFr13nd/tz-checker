"""Шифрование секретов через Windows DPAPI.

chmod(0o600) в Windows не защищает файл, поэтому API-ключ хранится в
settings.json в зашифрованном виде (CryptProtectData, привязка к учётной
записи Windows). На других ОС возвращается None — приложение работает как
раньше и предупреждает об этом в документации.
"""

from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1


def encryption_available() -> bool:
    return _IS_WINDOWS


def _protect(data: bytes) -> bytes | None:
    if not _IS_WINDOWS or not data:
        return None
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "TZ Checker API key",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, wintypes.HLOCAL))


def _unprotect(data: bytes) -> bytes | None:
    if not _IS_WINDOWS or not data:
        return None
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    description = wintypes.LPWSTR()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        ctypes.byref(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob_out.pbData, wintypes.HLOCAL))
        if description:
            _kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))


def encrypt_secret(value: str) -> str | None:
    """Вернуть base64(DPAPI) или None, если шифрование недоступно."""
    protected = _protect(value.encode("utf-8"))
    if protected is None:
        return None
    return base64.b64encode(protected).decode("ascii")


def decrypt_secret(token: str) -> str | None:
    try:
        raw = base64.b64decode(token.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    decoded = _unprotect(raw)
    if decoded is None:
        return None
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
