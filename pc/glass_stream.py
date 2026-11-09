"""
Glass Desktop — PC side (v0.2).

Captures your screen, shrinks it to the MemoMind glasses' size, dithers it to
green shades and streams it over a WebSocket. The phone passes each frame to
the glasses and asks for the next one.

Speed-ups in v0.2:
  * capture runs continuously in the background, so a fresh frame is ready
    the moment the phone asks (capture time no longer adds to the delay)
  * faster downscaling (shrinks in C before converting to grey)
  * only the changed part of the picture is sent (a still desktop with a
    moving mouse sends a tiny patch instead of the whole frame)

Run:   py glass_stream.py --monitor 3
Then:  tailscale serve --bg 8765
"""
import argparse
import asyncio
import ctypes
import json
import os
import secrets
import struct
import sys
import threading
import time

import mss
import numpy as np
import websockets
from PIL import Image, ImageFilter

import pc_audio
import pc_input
import screens

try:  # fast C version if available
    import lz4.block

    def lz4_compress(raw):
        return lz4.block.compress(raw, store_size=False)
except ImportError:  # pure-Python fallback (no compiler needed)
    from lz4_fallback import compress as lz4_compress

SCREEN_W, SCREEN_H = 600, 350
MAX_TILE_BYTES = 81901
BAYER = (np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32) + 0.5) / 16
MAGIC = b"GD"
VERSION = 2

DEFAULTS = {"size": "300x176", "levels": 4, "brightness": -30, "contrast": 1.2, "zoom": 1.0, "monitor": 1,
            "style": "shaded", "invert": "auto"}


def make_dpi_aware():
    """Without this, Windows display scaling (e.g. 150% on a 4K screen) makes the
    mouse position and zoom area land in the wrong place."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


SHARPEN = True


def new_mss():
    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def cursor_pos():
    """Mouse position in screen coordinates (Windows only)."""
    if sys.platform != "win32":
        return None
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def parse_size(size):
    w, h = (int(v) for v in str(size).lower().split("x"))
    w = max(2, min(SCREEN_W, w - (w % 2)))
    return w, max(1, min(SCREEN_H, h))


# ---------- which screen ----------
# Screens are identified by their Windows device name (e.g. \\.\DISPLAY3), which
# stays the same when Windows reorders them; the plain monitor number does not.

SCREEN_LIST = []   # full list for the phone, kept while other screens are switched off


def current_screens():
    """[{id, x, y, w, h, primary}] for the screens that are on right now."""
    if screens.ENABLED:
        try:
            return [{"id": s["name"], "x": s["x"], "y": s["y"], "w": s["w"], "h": s["h"], "primary": s["primary"]}
                    for s in screens.screens()]
        except Exception:
            pass
    with new_mss() as sct:
        return [{"id": f"mss:{i}", "x": m["left"], "y": m["top"], "w": m["width"], "h": m["height"],
                 "primary": i == 1} for i, m in enumerate(sct.monitors) if i]


def refresh_screen_list():
    global SCREEN_LIST
    found = current_screens()
    if len(found) >= len(SCREEN_LIST) or not SCREEN_LIST:
        SCREEN_LIST = found


def screen_choices():
    """Labels for the phone, left to right."""
    out = []
    for n, s in enumerate(sorted(SCREEN_LIST, key=lambda s: (s["x"], s["y"])), 1):
        shape = "portrait" if s["h"] > s["w"] else "landscape"
        out.append({"id": s["id"], "label": f"{n} · {s['w']}×{s['h']} {shape}" + (" · main" if s["primary"] else "")})
    return out


def default_screen(args):
    if args.monitor:   # --monitor N, as listed at start-up
        with new_mss() as sct:
            mons = sct.monitors
            m = mons[min(max(1, args.monitor), len(mons) - 1)]
        for s in SCREEN_LIST:
            if (s["x"], s["y"], s["w"], s["h"]) == (m["left"], m["top"], m["width"], m["height"]):
                return s["id"]
    return max(SCREEN_LIST, key=lambda s: s["w"] * s["h"])["id"] if SCREEN_LIST else None


def parse_res(value):
    """'1280x720' -> (1280, 720); 'native' / empty -> None."""
    try:
        w, h = (int(v) for v in str(value).lower().split("x"))
        return (w, h) if w >= 640 and h >= 360 else None
    except ValueError:
        return None


def rect_for(screen_id):
    """Where that screen is right now (positions change when others are switched off)."""
    for s in current_screens():
        if s["id"] == screen_id:
            return [s["x"], s["y"], s["w"], s["h"]]
    return None


# ---------- capture + encode ----------

def capture_gray(sct, cfg, out_w, out_h):
    """Grab the monitor (or a zoomed area around the cursor) as an out_w x out_h grey image."""
    if cfg.get("rect"):
        x, y, w, h = cfg["rect"]
        mon = {"left": x, "top": y, "width": w, "height": h}
    else:
        monitors = sct.monitors
        mon = monitors[min(max(1, int(cfg["monitor"])), len(monitors) - 1)]
    zoom = max(1.0, float(cfg["zoom"]))
    cur = cursor_pos()

    if zoom > 1.0:
        rw = int(mon["width"] / zoom)
        rh = min(int(rw * out_h / out_w), mon["height"])
        cx, cy = cur if cur else (mon["left"] + mon["width"] // 2, mon["top"] + mon["height"] // 2)
        left = min(max(cx - rw // 2, mon["left"]), mon["left"] + mon["width"] - rw)
        top = min(max(cy - rh // 2, mon["top"]), mon["top"] + mon["height"] - rh)
        region = {"left": left, "top": top, "width": rw, "height": rh}
    else:
        region = {k: mon[k] for k in ("left", "top", "width", "height")}

    shot = sct.grab(region)
    sw, sh = shot.width, shot.height
    scale = min(out_w / sw, out_h / sh)
    dw, dh = max(1, round(sw * scale)), max(1, round(sh * scale))

    img = Image.frombuffer("RGB", (sw, sh), shot.bgra, "raw", "BGRX", 0, 1)
    k = max(1, min(sw // (dw * 2), sh // (dh * 2)))  # cheap box-shrink first
    if k > 1:
        img = img.reduce(k)
    small = img.convert("L").resize((dw, dh), Image.LANCZOS)
    if SHARPEN:
        # shrinking a 4K screen ~10x turns text into grey mush; sharpening keeps edges
        small = small.filter(ImageFilter.UnsharpMask(radius=1.2, percent=160, threshold=2))
    small = np.asarray(small)

    canvas = np.zeros((out_h, out_w), dtype=np.uint8)
    ox, oy = (out_w - dw) // 2, (out_h - dh) // 2
    canvas[oy:oy + dh, ox:ox + dw] = small
    if cur:
        draw_cursor(canvas, int(ox + (cur[0] - region["left"]) * scale), int(oy + (cur[1] - region["top"]) * scale))
    return canvas


def draw_cursor(img, x, y):
    h, w = img.shape
    if not (0 <= x < w and 0 <= y < h):
        return
    r = max(3, w // 60)
    for val, grow in ((0, 1), (255, 0)):
        img[max(0, y - grow):y + grow + 1, max(0, x - r - grow):x + r + grow + 1] = val
        img[max(0, y - r - grow):y + r + grow + 1, max(0, x - grow):x + grow + 1] = val


def to_gray4(gray, levels, brightness, contrast, style="shaded", invert=False):
    """Packed GRAY_4 for the see-through green display.
    shaded: darker mid-tones + ordered dither (good for pictures and video)
    text:   no dither, stretched contrast, 4 clean steps (keeps letter edges)"""
    h, w = gray.shape
    if invert:
        gray = 255 - gray
    if style == "text":
        lo, hi = np.percentile(gray, (2, 99.5))
        span = max(24.0, float(hi - lo))
        v = np.arange(256, dtype=np.float32)
        t = np.clip(((v - lo) / span - 0.5) * contrast + 0.5 + brightness / 255, 0, 1)
        lut = (np.rint(t * 3) * 5).astype(np.uint8)  # 0, 5, 10, 15
        q = lut[gray]
        return ((q[:, 0::2] << 4) | q[:, 1::2]).astype(np.uint8)
    lut = np.clip(((np.arange(256, dtype=np.float32) - 128) * contrast + 128 + brightness) / 255, 0, 1) ** 2
    t = lut[gray]
    steps = max(1, levels - 1)
    bayer = np.tile(BAYER, (h // 4 + 1, w // 4 + 1))[:h, :w]
    q = np.clip(np.floor(t * steps + bayer), 0, steps)
    v = np.rint(q * 15 / steps).astype(np.uint8)
    return ((v[:, 0::2] << 4) | v[:, 1::2]).astype(np.uint8)


class AutoInvert:
    """Light screens (white pages, dark text) become a green wash on the glasses.
    Flip them so the text glows and the background stays clear. Hysteresis stops flicker."""

    def __init__(self):
        self.on = False

    def __call__(self, gray, mode):
        if mode == "on":
            return True
        if mode == "off":
            return False
        mean = float(gray.mean())
        if self.on and mean < 105:
            self.on = False
        elif not self.on and mean > 150:
            self.on = True
        return self.on


# ---------- messages ----------
# "GD" | u8 version=2 | u8 tiles | u16 0 | u16 0 | u16 0 | u16 0 | u32 seq
# per tile: u16 x | u16 y | u16 w | u16 rows | u32 decodedSize | u32 compLen | LZ4 block

def pack_tiles(rects, seq):
    """rects: list of (x, y, packed_region). Splits big regions so each fits one bridge call."""
    tiles = []
    for x, y, region in rects:
        h, stride = region.shape
        rows_per_tile = max(1, MAX_TILE_BYTES // stride)
        for ry in range(0, h, rows_per_tile):
            rows = min(rows_per_tile, h - ry)
            raw = region[ry:ry + rows].tobytes()
            tiles.append((x, y + ry, stride * 2, rows, len(raw), lz4_compress(raw)))
    parts = [MAGIC, struct.pack("<BBHHHHI", VERSION, len(tiles), 0, 0, 0, 0, seq)]
    for x, y, w, rows, size, comp in tiles:
        parts.append(struct.pack("<HHHHII", x, y, w, rows, size, len(comp)))
        parts.append(comp)
    return b"".join(parts)


def changed_rect(prev, cur):
    """Bounding box (in packed bytes) of what differs, or None if nothing changed."""
    diff = prev != cur
    rows = np.flatnonzero(diff.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(diff.any(axis=0))
    return rows[0], rows[-1] + 1, cols[0], cols[-1] + 1


# ---------- background capture ----------

class Grabber:
    """Captures and dithers frames continuously; the newest one is always ready."""

    def __init__(self, cfg, max_fps):
        self.cfg = dict(cfg)
        self.max_fps = max_fps
        self.lock = threading.Lock()
        self.latest = None          # (packed, w, h, frame_no, capture_ms)
        self.frame_no = 0
        self.running = True
        self.paused = False
        self.waiters = []           # (loop, asyncio.Event)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def set_cfg(self, cfg):
        with self.lock:
            self.cfg = dict(cfg)
            self.latest = None

    def run(self):
        sct = new_mss()
        auto_invert = AutoInvert()
        interval = 1.0 / self.max_fps
        while self.running:
            t0 = time.perf_counter()
            if self.paused:
                time.sleep(0.1)
                continue
            with self.lock:
                cfg = dict(self.cfg)
            try:
                w, h = parse_size(cfg["size"])
                gray = capture_gray(sct, cfg, w, h)
                packed = to_gray4(gray, int(cfg["levels"]), float(cfg["brightness"]), float(cfg["contrast"]),
                                  str(cfg.get("style", "shaded")), auto_invert(gray, str(cfg.get("invert", "auto"))))
            except Exception as error:  # e.g. monitor layout changed
                print("    capture error:", error)
                time.sleep(0.5)
                sct = new_mss()
                continue
            ms = (time.perf_counter() - t0) * 1000
            with self.lock:
                if cfg == self.cfg:
                    self.frame_no += 1
                    self.latest = (packed, w, h, self.frame_no, ms)
                waiters, self.waiters = self.waiters, []
            for loop, event in waiters:
                try:
                    loop.call_soon_threadsafe(event.set)
                except RuntimeError:  # the program is shutting down
                    self.running = False
            spare = interval - (time.perf_counter() - t0)
            if spare > 0:
                time.sleep(spare)

    async def next_after(self, frame_no):
        """Waits for a frame newer than frame_no."""
        loop = asyncio.get_running_loop()
        while True:
            with self.lock:
                if self.latest and self.latest[3] > frame_no:
                    return self.latest
                event = asyncio.Event()
                self.waiters.append((loop, event))
            await event.wait()

    def stop(self):
        self.running = False


# ---------- one-screen mode ----------

def monitor_count():
    try:
        return ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS
    except Exception:
        return -1


class SoloScreen:
    """Switches Windows to 'PC screen only' (just the main display) while a
    phone is connected, and back to 'Extend' when the last one disconnects.
    Same as Win+P. Make the screen you want your main display first."""

    def __init__(self, enabled, keep_rect=None):
        self.enabled = enabled and sys.platform == "win32"
        self.keep_rect = keep_rect
        self.keep_name = None
        self.keep_res = None
        self.used_api = False
        self.clients = 0
        self.active = False
        self.restore_task = None
        self.normal_count = monitor_count() if self.enabled else -1
        self.dark = False          # turn the physical monitors off while streaming
        self.darkened = set()      # which ones we switched off (to switch back on)

    def _run(self, arg):
        import subprocess
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "DisplaySwitch.exe")
        try:
            subprocess.run([exe, arg], check=False, timeout=10)
        except Exception as error:
            print("    display switch failed:", error)

    def _switch(self, mode, want_more):
        # Older Windows uses /internal and /extend, newer also accepts 1 and 3.
        for arg in ((f"/{mode}",) + (("1",) if mode == "internal" else ("3",))):
            before = monitor_count()
            self._run(arg)
            for _ in range(12):
                time.sleep(0.5)
                now = monitor_count()
                if (now > before) if want_more else (now < before or now == 1):
                    print(f"    screens now: {now}")
                    return True
        print(f"    display switch did not change anything (screens: {monitor_count()}) — use Win+P")
        return False

    def _power_off(self, names):
        try:
            done = screens.set_power(names, False)
        except Exception as error:
            print("    monitor power-off failed:", error)
            done = []
        self.darkened.update(done)
        if done:
            print(f"    monitors switched off: {len(done)}")
        elif names is None or names:
            print("    no monitor answered the power-off (DDC/CI may be off in the monitor's menu)")

    def _power_on(self):
        if not self.darkened:
            return
        for _ in range(2):   # some monitors miss the first command right after waking the signal
            try:
                screens.set_power(set(self.darkened), True)
            except Exception as error:
                print("    monitor power-on failed:", error)
            time.sleep(1.5)
        print(f"    monitors switched back on: {len(self.darkened)}")
        self.darkened.clear()

    async def set_dark(self, dark):
        """Phone toggled 'turn PC monitors off' while connected."""
        if dark == self.dark:
            return
        self.dark = dark
        if self.clients and sys.platform == "win32":
            if dark:
                await asyncio.to_thread(self._power_off, None)
            else:
                await asyncio.to_thread(self._power_on)

    async def switch_to(self, name, res=None):
        """The phone picked another screen or resolution while the others are off: redo the switch."""
        if not self.enabled or not self.active or (name == self.keep_name and res == self.keep_res):
            self.keep_name, self.keep_res = name, res
            return
        self.keep_name, self.keep_res = name, res
        print("    one-screen mode: applying the new screen / resolution")
        await asyncio.to_thread(self._restore_layout)
        await asyncio.sleep(1)
        self.used_api = await asyncio.to_thread(self._solo)
        if not self.used_api:
            await asyncio.to_thread(self._switch, "internal", False)
        self.active = True
        await asyncio.sleep(1.5)
        if self.dark:
            await asyncio.to_thread(self._power_off, None)

    async def connected(self, name=None, res=None, dark=None):
        if name:
            self.keep_name = name
        self.keep_res = res
        if dark is not None:
            self.dark = dark
        self.clients += 1
        if self.restore_task:
            self.restore_task.cancel()
            self.restore_task = None
            print("    reconnected — keeping the screen setup")
        if sys.platform != "win32":
            return
        if self.enabled and not self.active:
            self.normal_count = monitor_count()
            if self.dark:
                # switch the other monitors off first, so they don't sit there hunting for a signal
                others = {s["id"] for s in current_screens() if s["id"] != self.keep_name}
                await asyncio.to_thread(self._power_off, others)
            print("    one-screen mode: turning other screens off")
            self.used_api = await asyncio.to_thread(self._solo)
            if not self.used_api:
                await asyncio.to_thread(self._switch, "internal", False)
            self.active = True
            await asyncio.sleep(1.5)
        if self.dark:
            await asyncio.sleep(1)   # after any resolution change has settled
            await asyncio.to_thread(self._power_off, None)

    async def disconnected(self):
        self.clients = max(0, self.clients - 1)
        if self.clients == 0 and ((self.enabled and self.active) or self.darkened):
            print("    no phone connected — screens come back in 8 s")
            self.restore_task = asyncio.create_task(self._restore_later())

    async def _restore_later(self):
        try:
            await asyncio.sleep(8)
        except asyncio.CancelledError:
            return
        self.restore_task = None
        await asyncio.to_thread(self.restore_now)

    def _solo(self):
        try:
            ok = screens.solo(self.keep_rect, self.keep_name, self.keep_res)
        except Exception as error:
            print("    could not detach screens:", error)
            ok = False
        print(f"    screens now: {monitor_count()}" if ok else "    falling back to Win+P 'PC screen only'")
        return ok

    def restore_now(self):
        self._restore_layout()
        self._power_on()

    def _restore_layout(self):
        if self.enabled and self.active:
            print("    one-screen mode: turning other screens back on")
            ok = False
            if self.used_api:
                try:
                    ok = screens.restore()
                except Exception as error:
                    print("    restore error:", error)
                if ok:
                    print(f"    screens now: {monitor_count()}")
            if not ok:
                self._switch("extend", True)
            self.active = False


SOLO = SoloScreen(False)
PIN = ""
NO_PIN = False
DEFAULT_SCREEN = None
DEFAULT_RES = None
DEFAULT_DARK = False


def load_pin():
    """A 6-digit PIN stored next to the script. The phone must send it before it
    gets the picture or can control the mouse and keyboard."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glass_pin.txt")
    try:
        with open(path, encoding="utf-8") as f:
            pin = f.read().strip()
        if pin.isdigit() and len(pin) >= 4:
            return pin
    except OSError:
        pass
    pin = f"{secrets.randbelow(10**6):06d}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(pin)
    return pin


def handle_control(data):
    """Mouse / keyboard commands from the phone."""
    kind = data.get("type")
    if kind == "move":
        pc_input.move(data.get("dx", 0), data.get("dy", 0))
    elif kind == "click":
        pc_input.click(str(data.get("button", "left")), bool(data.get("double")))
    elif kind == "button":
        pc_input.button(str(data.get("button", "left")), bool(data.get("pressed")))
    elif kind == "scroll":
        pc_input.scroll(int(data.get("dy", 0)), int(data.get("dx", 0)))
    elif kind == "text":
        pc_input.type_text(str(data.get("text", "")))
    elif kind == "key":
        pc_input.combo(str(data.get("key", "")))


# ---------- server ----------

async def handle(ws, args):
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in vars(args).items() if k in DEFAULTS and v is not None})
    peer = getattr(ws, "remote_address", "?")
    first_raw = None
    try:
        first_raw = await asyncio.wait_for(ws.recv(), timeout=10)
        hello = json.loads(first_raw)
    except Exception:
        hello = {}
    if NO_PIN:
        pass  # anyone who can reach this PC through your tailnet may connect
    elif hello.get("type") != "hello" or str(hello.get("pin", "")).strip() != PIN:
        if hello.get("type") != "hello":
            why = f"no PIN sent (first message was '{hello.get('type', 'nothing')}') — the phone plugin is out of date, install 0.4.0"
        else:
            why = f"wrong PIN (phone sent {len(str(hello.get('pin', '')))} digits)"
        print(f"[!] rejected {peer}: {why}")
        await ws.send(json.dumps({"type": "error", "message": "Wrong PIN — check the PIN printed on the PC."}))
        await ws.close()
        return
    use_audio = pc_audio.AVAILABLE and not getattr(args, "no_audio", False)
    if not SOLO.active:
        refresh_screen_list()
    await ws.send(json.dumps({"type": "welcome", "control": pc_input.ENABLED, "audio": use_audio,
                              "screens": screen_choices(), "screen": DEFAULT_SCREEN}))
    print(f"[+] phone connected {peer}")
    # the phone answers with its settings (including which screen) before we switch anything
    early = []
    picked = None
    picked_res = DEFAULT_RES
    picked_dark = DEFAULT_DARK
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=3)
        early.append(msg)
        data = json.loads(msg) if isinstance(msg, str) else {}
        if data.get("type") == "config":
            if any(s["id"] == data.get("screen") for s in SCREEN_LIST):
                picked = data["screen"]
            if "res" in data:
                picked_res = parse_res(data["res"])
            if "dark" in data:
                picked_dark = bool(data["dark"])
    except Exception:
        pass
    screen_id = picked or DEFAULT_SCREEN
    await SOLO.connected(screen_id, picked_res, picked_dark)
    cfg["screen"] = screen_id
    cfg["rect"] = rect_for(screen_id) if screen_id else None
    grabber = Grabber(cfg, args.max_fps)
    loop = asyncio.get_running_loop()
    audio_q = asyncio.Queue()

    def push_audio(pkt):
        if audio_q.qsize() > 6:          # network can't keep up: drop old audio, keep latency low
            try:
                audio_q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        audio_q.put_nowait(pkt)

    async def audio_sender():
        while True:
            await ws.send(await audio_q.get())

    audio = pc_audio.AudioGrabber(lambda pkt: loop.call_soon_threadsafe(push_audio, pkt)) if use_audio else None
    audio_task = asyncio.create_task(audio_sender()) if audio else None
    ready = asyncio.Event()
    ready.set()
    state = {"clear": True, "sent": None, "geom": None}
    seq = 0
    last_frame = 0
    stats = {"frames": 0, "skipped": 0, "bytes": 0, "cap": 0.0, "rtt": 0.0, "t": time.time()}
    sent_at = 0.0

    replay = ([first_raw] if (first_raw is not None and hello.get("type") != "hello") else []) + early

    async def messages():
        for m in replay:
            yield m
        async for m in ws:
            yield m

    async def receiver():
        nonlocal sent_at
        async for msg in messages():
            if isinstance(msg, bytes):
                continue
            try:
                data = json.loads(msg)
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "ack":
                if sent_at:
                    stats["rtt"] += time.perf_counter() - sent_at
                ready.set()
            elif kind == "config":
                old = dict(cfg)
                for key in DEFAULTS:
                    if key in data:
                        cfg[key] = data[key]
                if "dark" in data:
                    await SOLO.set_dark(bool(data["dark"]))
                new_screen = data.get("screen") or cfg.get("screen")
                if not any(s["id"] == new_screen for s in SCREEN_LIST):
                    new_screen = cfg.get("screen")
                new_res = parse_res(data["res"]) if "res" in data else SOLO.keep_res
                if new_screen != cfg.get("screen") or (SOLO.enabled and new_res != SOLO.keep_res):
                    cfg["screen"] = new_screen
                    await SOLO.switch_to(new_screen, new_res)
                    cfg["rect"] = rect_for(new_screen)
                    state["clear"] = True
                    print(f"    screen -> {new_screen}  resolution -> {new_res or 'native'}")
                if cfg != old:
                    if cfg["size"] != old["size"]:
                        state["clear"] = True
                    state["sent"] = None
                    grabber.set_cfg(cfg)
                    print(f"    config {cfg}")
            elif kind == "pause":
                grabber.paused = bool(data.get("paused"))
            elif kind == "audio":
                if audio:
                    audio.enabled = bool(data.get("on", True))
            elif kind in ("move", "click", "button", "scroll", "text", "key"):
                try:
                    handle_control(data)
                except Exception as error:
                    print("    input error:", error)

    recv_task = asyncio.create_task(receiver())
    try:
        while not recv_task.done():
            if not ready.is_set():
                # wait for the phone's ack, but stop at once if the phone goes away
                ack = asyncio.create_task(ready.wait())
                await asyncio.wait({ack, recv_task}, return_when=asyncio.FIRST_COMPLETED)
                if not ack.done():
                    ack.cancel()
                    break
            if state["clear"]:
                state["clear"] = False
                state["sent"] = None
                seq += 1
                ready.clear()
                sent_at = 0.0
                await ws.send(pack_tiles([(0, 0, np.zeros((SCREEN_H, SCREEN_W // 2), np.uint8))], seq))
                continue

            wait = asyncio.create_task(grabber.next_after(last_frame))
            done, _ = await asyncio.wait({wait, recv_task}, return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:
                wait.cancel()
                break
            packed, w, h, last_frame, cap_ms = wait.result()
            x0, y0 = (SCREEN_W - w) // 2, (SCREEN_H - h) // 2

            prev = state["sent"]
            if prev is not None and prev.shape == packed.shape:
                box = changed_rect(prev, packed)
                if box is None:
                    stats["skipped"] += 1
                    continue  # identical: wait for the next capture
                r0, r1, c0, c1 = box
                rects = [(x0 + c0 * 2, y0 + r0, packed[r0:r1, c0:c1])]
            else:
                rects = [(x0, y0, packed)]

            seq += 1
            msg = pack_tiles(rects, seq)
            ready.clear()
            sent_at = time.perf_counter()
            await ws.send(msg)
            state["sent"] = packed
            stats["frames"] += 1
            stats["bytes"] += len(msg)
            stats["cap"] += cap_ms

            now = time.time()
            if now - stats["t"] >= 5:
                el = now - stats["t"]
                n = max(1, stats["frames"])
                print(f"    {stats['frames'] / el:4.1f} fps  {stats['bytes'] / el / 1024:5.1f} KB/s  "
                      f"capture {stats['cap'] / n:3.0f} ms  phone+glasses {stats['rtt'] / n * 1000:4.0f} ms  "
                      f"{cfg['size']} L{cfg['levels']} zoom {cfg['zoom']}")
                stats.update(frames=0, skipped=0, bytes=0, cap=0.0, rtt=0.0, t=now)
    except websockets.ConnectionClosed:
        pass
    finally:
        recv_task.cancel()
        grabber.stop()
        if audio:
            audio.stop()
            audio_task.cancel()
        print(f"[-] phone disconnected {peer}")
        await SOLO.disconnected()


HERE = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(HERE, "glass_stream.pid")
LOG_FILE = os.path.join(HERE, "glass_stream.log")
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                           "Glass Desktop.lnk")


def use_log_file_if_hidden():
    """pythonw has no console: send output to glass_stream.log instead."""
    if sys.stdout is None or sys.stderr is None:
        try:
            if os.path.getsize(LOG_FILE) > 1_000_000:
                os.remove(LOG_FILE)
        except OSError:
            pass
        log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        print(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


def install_autostart(extra_args):
    """Start hidden at Windows sign-in with the same options (Startup folder shortcut)."""
    import subprocess
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    script = os.path.abspath(__file__)
    args = " ".join([f'"{script}"'] + [a if " " not in a else f'"{a}"' for a in extra_args])
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
        "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:WD;"
        "$s.WindowStyle=7;$s.Description='Glass Desktop streamer';$s.Save()"
    )
    env = dict(os.environ, LNK=STARTUP_LNK, TGT=pythonw, ARGS=args, WD=HERE)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, check=True)
    print("Autostart installed:", STARTUP_LNK)
    print("  runs:", pythonw, args)
    print("Starting it now in the background. Output goes to", LOG_FILE)
    subprocess.Popen([pythonw, script, *extra_args], cwd=HERE,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def uninstall_autostart():
    try:
        os.remove(STARTUP_LNK)
        print("Autostart removed.")
    except FileNotFoundError:
        print("Autostart was not installed.")


def stop_running():
    """Stops the background copy (it restores the screens if needed)."""
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        print("Glass Desktop is not running in the background.")
        return
    import subprocess
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    if os.path.exists(screens.BACKUP):
        print("Restoring screens…", "ok" if screens.restore() else "failed — press Win+P → Extend")


async def main():
    p = argparse.ArgumentParser(description="Stream your PC screen to MemoMind glasses.")
    p.add_argument("--host", default="127.0.0.1", help="listen address (keep 127.0.0.1 and use tailscale serve)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--max-fps", type=float, default=20, help="capture rate cap")
    p.add_argument("--size", default=None, help="starting size, e.g. 300x176")
    p.add_argument("--levels", type=int, default=None)
    p.add_argument("--monitor", type=int, default=None, help="see the monitor list printed at start")
    p.add_argument("--res", default=None,
                   help="one-screen mode: switch the kept screen to this resolution while streaming, e.g. 1280x720 "
                        "(the phone app can change it)")
    p.add_argument("--screens-off", action="store_true",
                   help="switch the PC's monitors off while streaming (the phone app can change it)")
    p.add_argument("--no-sharpen", action="store_true", help="turn off edge sharpening")
    p.add_argument("--no-audio", action="store_true", help="don't send the PC's sound to the phone")
    p.add_argument("--install-autostart", action="store_true",
                   help="start hidden at every Windows sign-in with the other options given here")
    p.add_argument("--uninstall-autostart", action="store_true", help="remove the autostart")
    p.add_argument("--stop", action="store_true", help="stop the copy running in the background")
    p.add_argument("--restore-screens", action="store_true",
                   help="put the screens back as they were (if one-screen mode was interrupted) and exit")
    p.add_argument("--no-pin", action="store_true",
                   help="don't ask the phone for a PIN (only devices on your tailnet can reach the PC anyway)")
    p.add_argument("--one-screen", action="store_true",
                   help="turn other screens off while the phone is connected (uses Win+P 'PC screen only')")
    args = p.parse_args()
    global SOLO, PIN, NO_PIN, DEFAULT_SCREEN, DEFAULT_RES, DEFAULT_DARK
    if args.install_autostart:
        install_autostart([a for a in sys.argv[1:] if a != "--install-autostart"])
        return
    if args.uninstall_autostart:
        uninstall_autostart()
        return
    if args.stop:
        stop_running()
        return
    use_log_file_if_hidden()
    make_dpi_aware()
    if os.path.exists(screens.BACKUP):  # left over from an interrupted session
        print("Restoring screens from the last session…", "ok" if screens.restore() else "failed — press Win+P → Extend")
    if args.restore_screens:
        return
    keep_rect = None
    if args.monitor:
        with new_mss() as sct:
            mons = sct.monitors
            m = mons[min(max(1, args.monitor), len(mons) - 1)]
            keep_rect = (m["left"], m["top"], m["width"], m["height"])
    SOLO = SoloScreen(args.one_screen, keep_rect)
    refresh_screen_list()
    DEFAULT_SCREEN = default_screen(args)
    DEFAULT_RES = parse_res(args.res) if args.res else None
    DEFAULT_DARK = args.screens_off
    NO_PIN = args.no_pin
    PIN = "" if NO_PIN else load_pin()
    global SHARPEN
    SHARPEN = not args.no_sharpen
    make_dpi_aware()

    print(f"Glass Desktop v0.11 streaming on ws://{args.host}:{args.port}")
    with new_mss() as sct:
        print("Monitors:", ", ".join(f"{i}: {m['width']}x{m['height']}" for i, m in enumerate(sct.monitors) if i))
    print("Expose it to your phone with:  tailscale serve --bg", args.port)
    if NO_PIN:
        print("PIN check off: any device on your tailnet can view and control this PC.")
    else:
        print(f"PIN for the phone: {PIN}   (stored in glass_pin.txt — delete it for a new one)")
    print("Screens (pick one in the phone app):")
    for c in screen_choices():
        print("   ", c["label"], "  <- default" if c["id"] == DEFAULT_SCREEN else "")
    if SOLO.enabled:
        print("One-screen mode on: while the phone is connected only the picked screen stays on.")
    print("PC audio:", "on" if pc_audio.AVAILABLE and not args.no_audio else (pc_audio.WHY or "off"))
    try:
        server = await websockets.serve(lambda ws: handle(ws, args), args.host, args.port, max_size=2**20,
                                        compression=None, ping_interval=5, ping_timeout=10)
    except OSError:
        print(f"Port {args.port} is busy — Glass Desktop is probably already running "
              "(in the background?). Stop it with:  py glass_stream.py --stop")
        return
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    async with server:
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # a second Ctrl+C must not interrupt the screen restore
        SOLO.restore_now()  # never leave the screens off
        try:
            with open(PID_FILE, encoding="utf-8") as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(PID_FILE)
        except OSError:
            pass
