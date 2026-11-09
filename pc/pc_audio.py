"""Captures what the PC is playing (WASAPI loopback) for Glass Desktop.
Sends 24 kHz stereo 16-bit PCM in ~40 ms packets; the phone plays it through
whatever it uses for sound (pick the glasses as the output)."""
import struct
import sys
import threading
import warnings

try:
    import numpy as np
    import soundcard as sc
    AVAILABLE = sys.platform == "win32"
    WHY = "" if AVAILABLE else "PC audio only works on Windows"
except Exception as error:  # not installed / no audio device
    AVAILABLE = False
    WHY = f"PC audio off ({error.__class__.__name__}: {error}) — run: py -m pip install soundcard"

MAGIC = b"GA"
RATE_IN = 48000
RATE_OUT = 24000
CHANNELS = 2
BLOCK = 1920  # 40 ms at 48 kHz


def _lowpass_taps(n=63, cutoff=0.22):
    """Windowed-sinc low-pass (cutoff in cycles/sample at 48 kHz, ~10.5 kHz)."""
    m = np.arange(n) - (n - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * m) * np.blackman(n)
    return (h / h.sum()).astype(np.float32)


class Decimator:
    """48 kHz -> 24 kHz with a proper anti-alias filter that carries its state
    across blocks (plain averaging aliases and sounds watery/metallic)."""

    def __init__(self, channels):
        self.taps = _lowpass_taps()
        self.hist = np.zeros((len(self.taps) - 1, channels), dtype=np.float32)
        self.phase = 0

    def __call__(self, block):
        x = np.concatenate([self.hist, block.astype(np.float32)])
        self.hist = x[-(len(self.taps) - 1):]
        y = np.stack([np.convolve(x[:, c], self.taps, mode="valid") for c in range(x.shape[1])], axis=1)
        out = y[self.phase::2]
        self.phase = (self.phase + len(y)) % 2
        return out


class AudioGrabber:
    def __init__(self, on_packet):
        self.on_packet = on_packet      # called from the capture thread with bytes
        self.running = True
        self.enabled = True
        self.seq = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        import ctypes
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COM for this thread
        except Exception:
            pass
        warnings.filterwarnings("ignore", module="soundcard")
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            print(f"    audio: capturing '{speaker.name}'")
            decimate = Decimator(CHANNELS)
            with mic.recorder(samplerate=RATE_IN, channels=CHANNELS, blocksize=BLOCK) as rec:
                while self.running:
                    data = rec.record(numframes=BLOCK)  # waits while nothing is playing
                    if not self.enabled or data.size == 0:
                        continue
                    if data.ndim == 1:
                        data = np.stack([data, data], axis=1)
                    data = data[:, :CHANNELS]
                    half = decimate(data)                              # 48k -> 24k, filtered
                    if float(np.abs(half).max(initial=0)) < 1e-4:
                        continue                                       # silence: send nothing
                    pcm = np.clip(half * 32767, -32768, 32767).astype("<i2")
                    self.seq += 1
                    self.on_packet(MAGIC + struct.pack("<BBII", 1, CHANNELS, RATE_OUT, self.seq & 0xFFFFFFFF)
                                   + pcm.tobytes())
        except Exception as error:
            print("    audio stopped:", error)

    def stop(self):
        self.running = False
