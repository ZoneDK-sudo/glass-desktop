"""Turn every screen except one off (detach it from the desktop) and back on,
using the same Windows API as Settings > Display. The original layout is saved
to screens_backup.json first, so it can always be put back:
    py glass_stream.py --restore-screens
"""
import ctypes
import json
import os
import sys

BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screens_backup.json")
ENABLED = sys.platform == "win32"

DM_POSITION, DM_DISPLAYORIENTATION = 0x20, 0x80
DM_BITSPERPEL, DM_PELSWIDTH, DM_PELSHEIGHT, DM_DISPLAYFREQUENCY = 0x40000, 0x80000, 0x100000, 0x400000
CDS_UPDATEREGISTRY, CDS_SET_PRIMARY, CDS_NORESET = 0x1, 0x10, 0x10000000
ATTACHED, PRIMARY = 0x1, 0x4
ENUM_CURRENT = 0xFFFFFFFF

if ENABLED:
    from ctypes import wintypes

    class DEVMODEW(ctypes.Structure):  # display-device layout, 220 bytes
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
            ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
            ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
            ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
            ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
            ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
            ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
            ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
        ]

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    user32 = ctypes.windll.user32
    user32.ChangeDisplaySettingsExW.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p, wintypes.HWND,
                                                wintypes.DWORD, ctypes.c_void_p)


def _devmode():
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    return dm


def screens():
    """Attached displays: name, position, size, refresh, bits, orientation, primary."""
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        i += 1
        if not dd.StateFlags & ATTACHED:
            continue
        dm = _devmode()
        if not user32.EnumDisplaySettingsW(dd.DeviceName, ENUM_CURRENT, ctypes.byref(dm)):
            continue
        out.append({"name": dd.DeviceName, "x": dm.dmPositionX, "y": dm.dmPositionY,
                    "w": dm.dmPelsWidth, "h": dm.dmPelsHeight, "hz": dm.dmDisplayFrequency,
                    "bpp": dm.dmBitsPerPel, "orient": dm.dmDisplayOrientation,
                    "primary": bool(dd.StateFlags & PRIMARY)})
    return out


def _change(name, dm, flags):
    return user32.ChangeDisplaySettingsExW(name, ctypes.byref(dm) if dm is not None else None, None, flags, None)


def _apply():
    return user32.ChangeDisplaySettingsExW(None, None, None, 0, None)


def pick(keep_rect=None, keep_name=None):
    """The screen to keep: by device name, else matching keep_rect (left, top, w, h), else the largest."""
    all_screens = screens()
    if keep_name:
        for s in all_screens:
            if s["name"] == keep_name:
                return s, all_screens
    if keep_rect:
        for s in all_screens:
            if (s["x"], s["y"], s["w"], s["h"]) == tuple(keep_rect):
                return s, all_screens
    return max(all_screens, key=lambda s: s["w"] * s["h"]), all_screens


def modes(name):
    """Display modes the screen supports: (w, h, hz)."""
    out = set()
    i = 0
    while True:
        dm = _devmode()
        if not user32.EnumDisplaySettingsW(name, i, ctypes.byref(dm)):
            break
        if dm.dmBitsPerPel >= 24:
            out.add((dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency))
        i += 1
    return out


def best_mode(name, cur_w, cur_h, want):
    """Closest supported mode to want=(w, h) (a landscape size; flipped for portrait
    screens), preferring the screen's own shape and the highest refresh rate."""
    tw, th = want
    if cur_h > cur_w:
        tw, th = th, tw
    shape = cur_w / cur_h
    best = None
    for w, h, hz in modes(name):
        if (w > h) != (cur_w > cur_h):
            continue
        cost = abs(w * h - tw * th) / (tw * th) + 3 * abs(w / h - shape)
        key = (round(cost, 3), -hz)
        if best is None or key < best[0]:
            best = (key, w, h, hz)
    return (best[1], best[2], best[3]) if best else None


def solo(keep_rect=None, keep_name=None, res=None):
    """Keep one screen (made primary at 0,0), detach the rest. Returns True on success."""
    if not ENABLED:
        return False
    keep, all_screens = pick(keep_rect, keep_name)
    if len(all_screens) <= 1 and not res:
        return True
    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump(all_screens, f, indent=1)
    for s in all_screens:
        if s["name"] == keep["name"]:
            continue
        dm = _devmode()
        dm.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT  # zero size = detach
        _change(s["name"], dm, CDS_UPDATEREGISTRY | CDS_NORESET)
    dm = _devmode()
    user32.EnumDisplaySettingsW(keep["name"], ENUM_CURRENT, ctypes.byref(dm))
    dm.dmPositionX = dm.dmPositionY = 0
    dm.dmFields |= DM_POSITION
    if res:  # lower resolution while streaming, so text and buttons are bigger
        mode = best_mode(keep["name"], keep["w"], keep["h"], res)
        if mode and (mode[0], mode[1]) != (keep["w"], keep["h"]):
            dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency = mode
            dm.dmFields |= DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
    _change(keep["name"], dm, CDS_SET_PRIMARY | CDS_UPDATEREGISTRY | CDS_NORESET)
    result = _apply()
    return result == 0 and len(screens()) == 1


def restore():
    """Put back the layout saved by solo(). Returns True when every screen is back."""
    if not ENABLED or not os.path.exists(BACKUP):
        return False
    with open(BACKUP, encoding="utf-8") as f:
        saved = json.load(f)
    for s in sorted(saved, key=lambda s: not s["primary"]):  # primary first
        dm = _devmode()
        dm.dmFields = (DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL
                       | DM_DISPLAYFREQUENCY | DM_DISPLAYORIENTATION)
        dm.dmPositionX, dm.dmPositionY = s["x"], s["y"]
        dm.dmPelsWidth, dm.dmPelsHeight = s["w"], s["h"]
        dm.dmBitsPerPel, dm.dmDisplayFrequency, dm.dmDisplayOrientation = s["bpp"], s["hz"], s["orient"]
        flags = CDS_UPDATEREGISTRY | CDS_NORESET | (CDS_SET_PRIMARY if s["primary"] else 0)
        _change(s["name"], dm, flags)
    _apply()
    ok = len(screens()) >= len(saved)
    if ok:
        os.remove(BACKUP)
    return ok


# ---------- monitor power (DDC/CI) ----------
# Asks the monitor itself to switch off, like pressing its power button, while
# Windows keeps treating it as connected: no "searching for input" flicker, and
# the picture can still be captured. Needs DDC/CI (on by default on most
# monitors; some have it under a "DDC/CI" menu setting). TVs often ignore it.

VCP_POWER = 0xD6
POWER_ON, POWER_OFF = 1, 4

if ENABLED:
    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    class PHYSICAL_MONITOR(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("hPhysicalMonitor", wintypes.HANDLE), ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]

    MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(RECT), wintypes.LPARAM)


def _hmonitors():
    """{device name: HMONITOR} for screens that are on the desktop."""
    found = {}

    def cb(hmon, hdc, rect, data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            found[info.szDevice] = hmon
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    return found


def set_power(names, on):
    """Turn the named screens' monitors on/off over DDC/CI. Returns names that answered."""
    if not ENABLED:
        return []
    dxva2 = ctypes.windll.dxva2
    done = []
    for name, hmon in _hmonitors().items():
        if names is not None and name not in names:
            continue
        count = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)) or not count.value:
            continue
        arr = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
            continue
        ok = False
        for pm in arr:
            if dxva2.SetVCPFeature(pm.hPhysicalMonitor, VCP_POWER, POWER_ON if on else POWER_OFF):
                ok = True
        dxva2.DestroyPhysicalMonitors(count.value, arr)
        if ok:
            done.append(name)
    return done
