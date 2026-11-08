"""Thumbnails of windows for the viewer's list (wire kind 3: the window's ID, then a JPEG).

Like the Mac host: only while a device is connected, a window that changed (title or size) or is
the one being shown every pass, the rest now and then, and a device that joins gets them all.
grim captures one window by its stableId, also one on a workspace nobody is looking at.

A workspace's thumbnail is its monitor in miniature: the thumbnails of its windows, where the
windows are, the one focused last on top. It costs no capture of its own.
"""
import io
import logging
import shutil
import subprocess
import threading
import time

from . import wire

try:
    from PIL import Image
except ImportError:     # a workspace then shows the window focused last
    Image = None

log = logging.getLogger("sill.thumbs")

BOX = (416, 264)        # a window is fitted inside this many pixels (the Mac host: half of this)
QUALITY = "60"
PASS = 2.0              # seconds between two looks at the windows
STATIC = 12.0           # seconds between thumbnails of a window that did not change (the Mac host: 6;
                        # grim costs about 0.25 s of CPU per window on a 5K screen)


def available():
    return shutil.which("grim") is not None


def _capture(w):
    pw, ph = w["_px"]
    scale = min(BOX[0] / pw, BOX[1] / ph, 1.0)
    cmd = ["grim", "-T", w["_stable"], "-s", f"{scale:.4f}", "-t", "jpeg", "-q", QUALITY, "-"]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd
    r = subprocess.run(cmd, capture_output=True, timeout=5)
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None


def _compose(ws, windows, last):
    """The JPEG of workspace `ws` (a list entry) from its windows' (list entries) thumbnails."""
    cw, ch = ws["_canvas"]
    scale = min(BOX[0] / cw, BOX[1] / ch)
    size = (max(2, round(cw * scale)), max(2, round(ch * scale)))
    mine = sorted((w for w in windows if w["_ws"] == ws["_ws"] and w["id"] in last), key=lambda w: -w["_rank"])
    if not mine:
        return None
    if Image is None:
        return last[mine[-1]["id"]]
    canvas = Image.new("RGB", size, (24, 26, 32))
    for w in mine:
        x, y, rw, rh = w["_rect"]
        box = (round(x * scale), round(y * scale), max(1, round(rw * scale)), max(1, round(rh * scale)))
        shot = Image.open(io.BytesIO(last[w["id"]])).convert("RGB").resize(box[2:], Image.BILINEAR)
        canvas.paste(shot, box[:2])
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=int(QUALITY))
    return out.getvalue()


class Thumbnails:
    def __init__(self, host):
        self.host = host
        self.last = {}          # window ID -> its JPEG, for a device that joins
        self.stamps = {}        # window ID -> (its title and size, when its thumbnail was taken)
        self.made = {}          # workspace ID -> what its thumbnail was made from
        self.wake = threading.Event()
        self.thread = None

    def session_added(self, s):
        for wid, jpeg in list(self.last.items()):
            s.send(wire.pack(wire.THUMBNAIL, wire.thumbnail_blob(wid, jpeg)))
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="thumbnails", daemon=True)
            self.thread.start()
        self.wake.set()

    def _has_sessions(self):
        with self.host.lock:
            return bool(self.host.sessions)

    def _run(self):
        while True:
            self.wake.clear()
            if not self._has_sessions():
                self.wake.wait()
                continue
            try:
                self._pass()
            except Exception as e:      # a failed look must not end the thumbnails
                log.warning("thumbnails: %s", e)
            time.sleep(PASS)

    def _pass(self):
        entries = list(self.host.sources)
        sources = [w for w in entries if "_stable" in w]
        live = {w["id"] for w in entries}
        for d in (self.stamps, self.last, self.made):
            for gone in [k for k in d if k not in live]:
                del d[gone]
        active = self.host.active.get("window", {}).get("_0") if "window" in self.host.active else None
        for w in sources:
            if not self._has_sessions():
                return
            wid = w["id"]
            info = (w["title"], w["width"], w["height"])
            stamp = self.stamps.get(wid)
            if stamp and stamp[0] == info and wid != active and time.monotonic() - stamp[1] < STATIC:
                continue
            self.stamps[wid] = (info, time.monotonic())     # before the capture: one that fails backs off too
            try:
                jpeg = _capture(w)
            except (OSError, subprocess.SubprocessError) as e:
                log.debug("thumbnail of %s: %s", w["title"], e)
                continue
            if jpeg and jpeg != self.last.get(wid):
                self._set(wid, jpeg)
        for ws in (w for w in entries if "_canvas" in w):
            mine = [w for w in sources if w["_ws"] == ws["_ws"] and w["id"] in self.last]
            made = tuple((w["id"], w["_rect"], w["_rank"], hash(self.last[w["id"]])) for w in mine)
            if made and made != self.made.get(ws["id"]):
                self.made[ws["id"]] = made
                jpeg = _compose(ws, sources, self.last)
                if jpeg:
                    self._set(ws["id"], jpeg)

    def _set(self, wid, jpeg):
        if jpeg != self.last.get(wid):
            self.last[wid] = jpeg
            self.host.broadcast(wire.pack(wire.THUMBNAIL, wire.thumbnail_blob(wid, jpeg)))
