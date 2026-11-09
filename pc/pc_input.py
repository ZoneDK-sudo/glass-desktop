"""Mouse and keyboard injection for Glass Desktop (Windows SendInput)."""
import ctypes
import sys

ENABLED = sys.platform == "win32"

if ENABLED:
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _send = ctypes.windll.user32.SendInput
    _send.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _send.restype = wintypes.UINT

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOVE, LDOWN, LUP, RDOWN, RUP, MDOWN, MUP, WHEEL, HWHEEL = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x800, 0x1000
KEYUP, UNICODE, EXTENDED = 0x2, 0x4, 0x1

VK = {
    "enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "delete": 0x2E,
    "win": 0x5B, "ctrl": 0x11, "alt": 0x12, "shift": 0x10,
    "f4": 0x73, "f5": 0x74, "f11": 0x7A,
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
}
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B}


def _guard(fn):
    def wrapper(*args, **kwargs):
        if ENABLED:
            return fn(*args, **kwargs)
        return None
    wrapper.__name__ = fn.__name__
    return wrapper


def _mouse(flags, dx=0, dy=0, data=0):
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    return i


def _key(vk, up=False):
    flags = (KEYUP if up else 0) | (EXTENDED if vk in EXTENDED_KEYS else 0)
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return i


def _uni(ch, up=False):
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(0, ord(ch), UNICODE | (KEYUP if up else 0), 0, 0)
    return i


def _go(*inputs):
    if not ENABLED or not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    _send(len(inputs), arr, ctypes.sizeof(INPUT))


@_guard
def move(dx, dy):
    _go(_mouse(MOVE, int(dx), int(dy)))


BUTTONS = {"left": (LDOWN, LUP), "right": (RDOWN, RUP), "middle": (MDOWN, MUP)}


@_guard
def click(button="left", double=False):
    down, up = BUTTONS.get(button, BUTTONS["left"])
    seq = [_mouse(down), _mouse(up)]
    _go(*(seq * (2 if double else 1)))


@_guard
def button(button="left", pressed=True):
    down, up = BUTTONS.get(button, BUTTONS["left"])
    _go(_mouse(down if pressed else up))


@_guard
def scroll(dy=0, dx=0):
    events = []
    if dy:
        events.append(_mouse(WHEEL, data=int(dy)))
    if dx:
        events.append(_mouse(HWHEEL, data=int(dx)))
    _go(*events)


@_guard
def type_text(text):
    events = []
    for ch in text[:500]:
        if ch == "\n":
            events += [_key(VK["enter"]), _key(VK["enter"], True)]
        elif ord(ch) > 0xFFFF:  # emoji etc: send as a UTF-16 surrogate pair
            code = ord(ch) - 0x10000
            for half in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                events += [_uni(chr(half)), _uni(chr(half), True)]
        else:
            events += [_uni(ch), _uni(ch, True)]
    _go(*events)


@_guard
def combo(spec):
    """'enter', 'ctrl+c', 'alt+tab', 'win+d' ..."""
    keys = [VK[k] for k in spec.lower().split("+") if k in VK]
    if not keys:
        return
    _go(*[_key(k) for k in keys], *[_key(k, True) for k in reversed(keys)])
