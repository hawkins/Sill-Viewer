"""Icons for the viewer's window list (wire kind 4: a 64 px PNG per bundleID).

An app's icon comes from its desktop file (matched to the window's class by StartupWMClass or the
file's name), else from an icon named like the class. Without one, a badge with the class's first
letter. A workspace gets a badge with its name, a monitor a drawn monitor.
"""
import colorsys
import glob
import io
import logging
import os
import time
import zlib

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gio, GdkPixbuf  # noqa: E402

try:
    import cairo
except ImportError:     # the badges and the monitor need it; app icons do not
    cairo = None

log = logging.getLogger("sill.icons")

SIZE = 64
INDEX_EVERY = 30        # seconds between two rescans of the desktop files, when an app is not in them

_cache = {}
_index = {}
_indexed = 0.0


def icon_png(spec):
    """PNG bytes for ("app", class), ("workspace", name), ("display",) or ("virtual",); None when
    there is no icon to draw."""
    if spec not in _cache:
        try:
            _cache[spec] = _render(spec)
        except Exception as e:  # a broken icon file must not break the window list
            log.warning("icon for %s: %s", spec, e)
            _cache[spec] = None
    return _cache[spec]


def _render(spec):
    kind = spec[0]
    if kind == "app":
        return _app(spec[1])
    if kind == "workspace":
        return _badge(spec[1][:3] if len(spec[1]) <= 3 else spec[1][:2], _hue(spec[1]))
    if kind in ("display", "virtual"):
        return _monitor(plus=kind == "virtual")
    return None


# --- apps -----------------------------------------------------------------------------------
def _desktop_index():
    """Lowercase class, desktop file id, and executable -> that file's Icon (a name or a path). The
    class and the id come first: Steam's shortcuts for games run `steam`, and would take that name."""
    global _index, _indexed
    strong, weak = {}, {}
    for app in Gio.DesktopAppInfo.get_all():
        icon = app.get_string("Icon")
        if not icon:
            continue
        ident = (app.get_id() or "").removesuffix(".desktop")
        for k in (app.get_string("StartupWMClass"), ident):
            if k:
                strong.setdefault(k.lower(), icon)
        for k in (ident.rsplit(".", 1)[-1], os.path.basename(app.get_executable() or "")):
            if k:
                weak.setdefault(k.lower(), icon)
    _index, _indexed = {**weak, **strong}, time.monotonic()
    return _index


def _app(cls):
    index = _index if _index and time.monotonic() - _indexed < INDEX_EVERY else _desktop_index()
    names = [index.get(cls.lower()), cls, cls.lower()]
    for name in names:
        path = _find(name) if name else None
        if path:
            png = _load(path)
            if png:
                return png
    return _badge((cls or "?")[:1].upper(), _hue(cls))


def _bases():
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [data] + [d for d in dirs.split(":") if d]


def _find(name):
    """The file of the icon called name: a path as it is, else the best of the icon themes' files
    (hicolor first, a scalable file, else the PNG closest above 64 px)."""
    if os.path.isabs(name):
        return name if os.path.isfile(name) else None
    best, best_rank = None, None
    for base in _bases() + [os.path.expanduser("~/.icons")]:
        root = base if base.endswith(".icons") else os.path.join(base, "icons")
        pats = [f"{root}/*/*/apps/{name}.*", f"{root}/*/apps/*/{name}.*", f"{root}/{name}.*"]
        for path in (p for pat in pats for p in glob.glob(pat)):
            ext = path.rsplit(".", 1)[-1]
            if ext not in ("png", "svg") or "symbolic" in path:
                continue
            size = 0 if ext == "svg" else _size_of(path)
            rank = ("/hicolor/" not in path, ext != "svg", size < SIZE, abs(size - SIZE))
            if best_rank is None or rank < best_rank:
                best, best_rank = path, rank
    if best is None:
        for ext in ("png", "svg"):
            p = f"/usr/share/pixmaps/{name}.{ext}"
            if os.path.isfile(p):
                return p
    return best


def _size_of(path):
    for part in path.split("/"):
        head = part.split("x")[0]
        if head.isdigit():
            return int(head)
    return 0


def _load(path):
    pix = GdkPixbuf.Pixbuf.new_from_file_at_size(path, SIZE, SIZE)
    ok, data = pix.save_to_bufferv("png", [], [])
    return bytes(data) if ok else None


# --- drawn icons ----------------------------------------------------------------------------
def _hue(key):
    key = str(key)
    if key.isdigit():
        return (int(key) * 0.381966) % 1      # the golden angle: workspace 1, 2, 3... are far apart
    return (zlib.crc32(key.encode()) % 360) / 360


def _rounded(c, x, y, w, h, r):
    c.new_sub_path()
    c.arc(x + w - r, y + r, r, -1.5708, 0)
    c.arc(x + w - r, y + h - r, r, 0, 1.5708)
    c.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    c.arc(x + r, y + r, r, 3.1416, 4.7124)
    c.close_path()


def _png(surface):
    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue()


def _badge(text, hue):
    """A rounded square in the colour of `hue`, with text in it."""
    if cairo is None:
        return None
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIZE, SIZE)
    c = cairo.Context(s)
    r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.62)
    _rounded(c, 2, 2, SIZE - 4, SIZE - 4, 14)
    c.set_source_rgb(r, g, b)
    c.fill()
    c.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    c.set_font_size(40 if len(text) == 1 else 32 if len(text) == 2 else 24)
    ext = c.text_extents(text)
    c.set_source_rgb(1, 1, 1)
    c.move_to(SIZE / 2 - ext.x_bearing - ext.width / 2, SIZE / 2 - ext.y_bearing - ext.height / 2)
    c.show_text(text)
    return _png(s)


def _monitor(plus):
    if cairo is None:
        return None
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIZE, SIZE)
    c = cairo.Context(s)
    _rounded(c, 4, 8, SIZE - 8, 38, 5)
    c.set_source_rgb(0.85, 0.87, 0.9)
    c.fill()
    _rounded(c, 8, 12, SIZE - 16, 30, 3)
    c.set_source_rgb(0.16, 0.2, 0.3)
    c.fill()
    c.set_source_rgb(0.85, 0.87, 0.9)
    c.rectangle(SIZE / 2 - 3, 46, 6, 8)
    c.fill()
    _rounded(c, SIZE / 2 - 12, 53, 24, 4, 2)
    c.fill()
    if plus:
        c.set_source_rgb(0.85, 0.87, 0.9)
        c.set_line_width(4)
        c.move_to(SIZE / 2 - 8, 27)
        c.line_to(SIZE / 2 + 8, 27)
        c.move_to(SIZE / 2, 19)
        c.line_to(SIZE / 2, 35)
        c.stroke()
    return _png(s)
