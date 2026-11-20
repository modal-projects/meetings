import threading
from pathlib import Path

W, H, FPS = 1280, 720, 10
_base = None


def base() -> bytes:
    global _base
    if _base is None:
        path = Path("/meetings/web/hal.yuv")
        if not path.exists():
            path = Path(__file__).parent / "web/hal.yuv"
        _base = path.read_bytes()
    return _base

FONT = {
    " ": [0, 0, 0, 0, 0, 0, 0],
    "A": [0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "B": [0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110],
    "C": [0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110],
    "D": [0b11100, 0b10010, 0b10001, 0b10001, 0b10001, 0b10010, 0b11100],
    "E": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111],
    "F": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000],
    "G": [0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01110],
    "H": [0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "I": [0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "J": [0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100],
    "K": [0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001],
    "L": [0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111],
    "M": [0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001],
    "N": [0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001],
    "O": [0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "P": [0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000],
    "Q": [0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101],
    "R": [0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001],
    "S": [0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110],
    "T": [0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100],
    "U": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "V": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100],
    "W": [0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001],
    "X": [0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001],
    "Y": [0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100],
    "Z": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111],
    ".": [0, 0, 0, 0, 0, 0b01100, 0b01100],
    "'": [0b00100, 0b00100, 0b01000, 0, 0, 0, 0],
    "-": [0, 0, 0, 0b01110, 0, 0, 0],
    ":": [0, 0b01100, 0b01100, 0, 0b01100, 0b01100, 0],
    "…": [0, 0, 0, 0, 0, 0b10101, 0],
    "0": [0b01110, 0b10011, 0b10101, 0b10101, 0b11001, 0b10001, 0b01110],
    "1": [0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "2": [0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111],
    "3": [0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110],
    "4": [0b00010, 0b00110, 0b01010, 0b11111, 0b00010, 0b00010, 0b00010],
    "5": [0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110],
    "6": [0b01110, 0b10000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110],
    "7": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000],
    "8": [0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110],
    "9": [0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00001, 0b01110],
}

BAR_TOP = H - 96
SCALE = 5


def frame(status: str, tick: int = 0) -> bytes:
    raw = base()
    glow = 0.7 + 0.3 * abs((tick % (FPS * 2)) / FPS - 1)
    lut = bytes(min(255, round(i * glow)) for i in range(256))
    y = bytearray(raw[: W * H].translate(lut))
    uv = raw[W * H :]
    for yoff in range(BAR_TOP, H):
        y[yoff * W : (yoff + 1) * W] = b"\x10" * W
    text = status.upper()
    width = len(text) * 6 * SCALE
    x0 = max(0, (W - width) // 2)
    y0 = BAR_TOP + (H - BAR_TOP - 7 * SCALE) // 2
    for i, ch in enumerate(text):
        glyph = FONT.get(ch, FONT[" "])
        for row, bits in enumerate(glyph):
            for col in range(5):
                if not bits & (1 << (4 - col)):
                    continue
                for dy in range(SCALE):
                    for dx in range(SCALE):
                        yy = y0 + row * SCALE + dy
                        xx = x0 + (i * 6 + col) * SCALE + dx
                        if 0 <= yy < H and 0 <= xx < W:
                            y[yy * W + xx] = 235
    return b"FRAME\n" + bytes(y) + uv


class Feed:
    def __init__(self, path: Path):
        self.path = path
        self.status = None
        self.lock = threading.Lock()
        self.set("LISTENING")

    def set(self, status: str) -> None:
        with self.lock:
            if status == self.status:
                return
            self.status = status
            loop = b"".join(frame(status, tick) for tick in range(FPS * 2))
            with open(self.path, "wb") as pipe:
                pipe.write(
                    f"YUV4MPEG2 W{W} H{H} F{FPS}:1 Ip A0:0 C420jpeg\n".encode()
                )
                pipe.write(loop)

    def close(self) -> None:
        pass


def feed_path(name: str) -> Path:
    return Path(f"/tmp/hal-{name}.y4m")
