import base64
import gzip
import json
import math
import re
import struct
import threading
from pathlib import Path

import wasmtime

SR = 48000
BLOCK = 800
REC = 272
MAX_SCHEDULE = 4000

CONTROL_KEYS = (
    "jaw", "tbX", "tbY", "ttX", "ttY", "ttCurl", "root", "lipAp", "lipProt", "lipWidth",
    "labiodental", "velum", "larynx", "epilarynx", "ventricular", "lateral", "dorsum",
    "abduction", "closure", "breath", "f0", "vibrato",
)
KEY_INDEX = {k: i for i, k in enumerate(CONTROL_KEYS)}
BODY = {"female": 0.0, "child": 0.0}

_engine = wasmtime.Engine()
_module = None
_module_lock = threading.Lock()
_dict_text = None


def module():
    global _module
    with _module_lock:
        if _module is None:
            _module = wasmtime.Module(
                _engine, (Path(__file__).parent / "tts" / "chimera.wasm").read_bytes()
            )
    return _module


def dictionary():
    global _dict_text
    if _dict_text is None:
        path = Path(__file__).parent / "tts" / "cmudict.txt.gz"
        _dict_text = gzip.decompress(path.read_bytes()).decode()
    return _dict_text


threading.Thread(target=lambda: (module(), dictionary()), daemon=True).start()


def default_f0(body):
    f = min(1.0, max(0.0, body.get("female", 0.0)))
    c = min(1.0, max(0.0, body.get("child", 0.0)))
    w = ((1 - c) * (1 - f), (1 - c) * f, c)
    return math.exp(sum(wi * math.log(f0) for wi, f0 in zip(w, (110.0, 205.0, 260.0))))


class Voice:
    def __init__(self):
        self.store = wasmtime.Store(_engine)
        inst = wasmtime.Instance(self.store, module(), [])
        self.ex = inst.exports(self.store)
        self.mem = self.ex["memory"]
        self.h = self.ex["engine_new"](self.store, float(SR))
        self.ex["engine_set_body"](self.store, self.h, BODY["female"], BODY["child"])
        self.call("g2pLoad", {"text": dictionary()})

    def call(self, fn, args):
        b = json.dumps({"fn": fn, **args}).encode()
        p = self.ex["chimera_alloc"](self.store, len(b))
        self.mem.write(self.store, b, p)
        status = self.ex["chimera_call"](self.store, p, len(b))
        self.ex["chimera_free"](self.store, p, len(b))
        out = bytes(
            self.mem.read(
                self.store,
                self.ex["chimera_result_ptr"](self.store),
                self.ex["chimera_result_ptr"](self.store) + self.ex["chimera_result_len"](self.store),
            )
        )
        v = json.loads(out)
        if status != 0:
            raise ValueError(f"engine {fn}: {v.get('error')}")
        return v

    def schedule(self, gestures, lead):
        gestures = gestures[:MAX_SCHEDULE]
        buf = bytearray(len(gestures) * REC)
        for i, g in enumerate(gestures):
            o = i * REC
            head = (g.get("t0"), g.get("t1"), g.get("rampIn"), g.get("rampOut"),
                    g.get("strength"), g.get("speed"), g.get("releaseHold"), g.get("tag"))
            struct.pack_into("<8d", buf, o, *(x if x is not None else math.nan for x in head))
            struct.pack_into("<22d", buf, o + 64, *([math.nan] * 22))
            for k, v in (g.get("targets") or {}).items():
                if k in KEY_INDEX and v is not None:
                    struct.pack_into("<d", buf, o + 64 + 8 * KEY_INDEX[k], v)
        self.mem.write(self.store, bytes(buf), self.ex["engine_records"](self.store, self.h))
        return self.ex["engine_schedule"](self.store, self.h, len(gestures), lead, 1)

    def render(self, n=BLOCK):
        p = self.ex["engine_render"](self.store, self.h, n)
        return struct.unpack(f"<{n}f", bytes(self.mem.read(self.store, p, p + n * 4)))

    def clock(self):
        return self.ex["engine_clock"](self.store, self.h)


_voice = None


def say(text):
    """text -> int16 mono PCM at 48kHz"""
    global _voice
    if _voice is None:
        _voice = Voice()
    return say_with(_voice, text)


def say_fresh(text):
    return say_with(Voice(), text)


def say_with(voice, text):
    phrases = voice.call("g2pPhrases", {"text": text.replace("\n", " ")})
    if not phrases:
        return b""
    plan = voice.call(
        "planPhrases",
        {
            "phrases": phrases,
            "options": {
                "body": BODY,
                "t0": 0,
                "rate": 1,
                "f0": default_f0(BODY),
                "accent": None,
                "sung": None,
            },
        },
    )
    off = voice.schedule(plan["gestures"], 0.03)
    end = off + plan["end"] + 0.25
    pcm = bytearray()
    while voice.clock() < end:
        for v in voice.render(BLOCK):
            if math.isnan(v):
                v = 0.0
            pcm += struct.pack("<h", max(-32768, min(32767, int(v * 0.8 * 32767))))
    return bytes(pcm)
