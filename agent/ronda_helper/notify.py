"""Pemberitahuan di layar user setelah screenshot diambil (kebijakan screenshot: user diberi tahu bila Admin memilih),
dan saat sesi remote dimulai (selalu).

Jendela Win32 kecil di pojok kanan bawah area kerja, selalu di atas, TIDAK mengambil fokus (WS_EX_NOACTIVATE +
SW_SHOWNOACTIVATE: ketikan user di aplikasi lain tidak terganggu), hilang sendiri setelah `seconds` atau saat diklik.
Bukan toast Windows: toast bisa disembunyikan Focus Assist / Jangan Ganggu, dan butuh registrasi AppUserModelID.
Ditampilkan SETELAH gambar diambil, jadi tidak ikut terekam. Tiap pemberitahuan berjalan di thread sendiri dengan
message loop-nya sendiri, sehingga helper tetap melayani pipe.
"""
import logging
import sys
import threading
import time

log = logging.getLogger(__name__)

TITLE = "Ronda · Screenshot layar"
REMOTE_TITLE = "Ronda · Remote layar"
SECONDS = 10


def message(now: float | None = None) -> str:
    return (f"Tim IT mengambil gambar layar komputer ini pukul {time.strftime('%H:%M', time.localtime(now))}. "
            "Permintaan ini tercatat di Audit Log Ronda.")


def show(text: str, seconds: int = SECONDS, title: str = TITLE) -> None:
    """Tampilkan pemberitahuan tanpa menunggu; di luar Windows hanya dicatat di log (development/test)."""
    if sys.platform != "win32":
        log.info("pemberitahuan: %s", text)
        return
    threading.Thread(target=_run_safe, args=(text, seconds, title), name="notify", daemon=True).start()


def _run_safe(text: str, seconds: int, title: str) -> None:
    try:
        _Popup(text, seconds, title).run()
    except Exception:
        log.exception("pemberitahuan screenshot gagal ditampilkan")


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                    ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]

    # Tipe argumen/hasil eksplisit: handle 64-bit tidak boleh terpotong menjadi int 32-bit
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.BeginPaint.restype = wintypes.HDC
    user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT),
                                 wintypes.UINT]
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    user32.LoadCursorW.restype = wintypes.HANDLE
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi32.CreateFontW.restype = wintypes.HFONT
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    WS_POPUP, WS_BORDER = 0x80000000, 0x00800000
    WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
    SW_SHOWNOACTIVATE, SPI_GETWORKAREA = 4, 0x30
    WM_DESTROY, WM_PAINT, WM_TIMER, WM_LBUTTONUP, WM_MOUSEACTIVATE = 0x2, 0xF, 0x113, 0x202, 0x21
    MA_NOACTIVATE, IDC_ARROW, TRANSPARENT = 3, 32512, 1
    DT_WORDBREAK, DT_NOPREFIX = 0x10, 0x800
    FW_NORMAL, FW_BOLD, CLEARTYPE_QUALITY = 400, 700, 5
    BG, FG, ACCENT = 0x003B291E, 0x00F5F5F5, 0x0060A5FA        # COLORREF 0x00BBGGRR: slate-800, putih, biru
    CLASS_NAME = "RondaScreenshotNotice"

    _texts: dict[int, tuple[str, str]] = {}                  # hwnd → (judul, isi)
    _class_lock = threading.Lock()
    _class_ready = False

    def _rgb_brush(color: int):
        return gdi32.CreateSolidBrush(color)

    @WNDPROC
    def _wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            _paint(hwnd)
            return 0
        if msg in (WM_TIMER, WM_LBUTTONUP):
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE                       # klik menutup jendela tanpa merebut fokus
        if msg == WM_DESTROY:
            _texts.pop(hwnd or 0, None)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _scale() -> float:
        try:
            return max(1.0, user32.GetDpiForSystem() / 96)     # Windows 10 1607+
        except AttributeError:
            return 1.0

    def _font(height: int, weight: int):
        return gdi32.CreateFontW(-height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, CLEARTYPE_QUALITY, 0, "Segoe UI")

    def _paint(hwnd) -> None:
        title, body = _texts.get(hwnd or 0, ("", ""))
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            s = _scale()
            rect = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            brush = _rgb_brush(BG)
            user32.FillRect(hdc, ctypes.byref(rect), brush)
            gdi32.DeleteObject(brush)
            bar = wintypes.RECT(0, 0, round(5 * s), rect.bottom)          # garis aksen kiri
            brush = _rgb_brush(ACCENT)
            user32.FillRect(hdc, ctypes.byref(bar), brush)
            gdi32.DeleteObject(brush)
            gdi32.SetBkMode(hdc, TRANSPARENT)
            gdi32.SetTextColor(hdc, FG)
            pad = round(16 * s)
            bold, normal = _font(round(15 * s), FW_BOLD), _font(round(14 * s), FW_NORMAL)
            old = gdi32.SelectObject(hdc, bold)
            area = wintypes.RECT(pad + round(4 * s), pad - round(4 * s), rect.right - pad, rect.bottom)
            user32.DrawTextW(hdc, title, -1, ctypes.byref(area), DT_NOPREFIX)
            gdi32.SelectObject(hdc, normal)
            area.top += round(26 * s)
            user32.DrawTextW(hdc, body, -1, ctypes.byref(area), DT_WORDBREAK | DT_NOPREFIX)
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(bold)
            gdi32.DeleteObject(normal)
        finally:
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def _register_class(hinst) -> None:
        global _class_ready
        with _class_lock:
            if _class_ready:
                return
            wc = WNDCLASSW()
            wc.lpfnWndProc = _wndproc
            wc.hInstance = hinst
            wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
            wc.lpszClassName = CLASS_NAME
            if not user32.RegisterClassW(ctypes.byref(wc)) and ctypes.get_last_error() != 1410:  # sudah terdaftar
                raise ctypes.WinError(ctypes.get_last_error())
            _class_ready = True

    class _Popup:
        def __init__(self, text: str, seconds: int, title: str = TITLE):
            self.text, self.seconds, self.title = text, seconds, title

        def run(self) -> None:
            hinst = kernel32.GetModuleHandleW(None)
            _register_class(hinst)
            s = _scale()
            width, height = round(400 * s), round(104 * s)
            work = wintypes.RECT()
            user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
            margin = round(16 * s)
            hwnd = user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, CLASS_NAME, self.title, WS_POPUP | WS_BORDER,
                work.right - width - margin, work.bottom - height - margin, width, height, None, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            _texts[hwnd] = (self.title, self.text)
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            user32.UpdateWindow(hwnd)
            user32.SetTimer(hwnd, 1, self.seconds * 1000, None)
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
