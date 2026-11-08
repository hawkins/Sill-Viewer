"""Hyprland (and other wlroots-style Wayland desktops): the same interface as mutter.py.

Hyprland has no GNOME-style remote desktop API, so the pieces come from elsewhere:
- the screen: the xdg-desktop-portal ScreenCast portal (xdg-desktop-portal-hyprland) into a
  PipeWire stream. Its picker would ask on this computer's screen; sill-linux-picker answers it
  with the monitor Sill wants (install.sh sets it as xdph's custom_picker_binary). Without it, the
  picker shows once per monitor and the portal's restore token skips it after that.
- input: a virtual mouse (absolute, like a VM tablet) and keyboard through /dev/uinput.
- monitors: hyprctl, and Hyprland's event socket for changes.
- clipboard: wl-clipboard (wl-paste --watch, wl-copy).
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import zlib

from gi.repository import Gio, GLib

from .mutter import (AXIS_FINISH_X, AXIS_FINISH_Y, BTN_LEFT, BTN_MIDDLE, BTN_RIGHT,  # noqa: F401
                     CURSOR_EMBEDDED, CURSOR_HIDDEN, CURSOR_METADATA, TEXT_MIMES, Monitor, session_bus)
from .identity import config_dir

log = logging.getLogger("sill.hypr")

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SC_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
PORTAL_CURSOR = {CURSOR_HIDDEN: 1, CURSOR_EMBEDDED: 2, CURSOR_METADATA: 4}
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
ABS_MAX = 65535


def available(bus=None):
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None


def _hyprctl_json(*args):
    out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=5).stdout
    return json.loads(out)


def _logical_size(m):
    w, h = m["width"] / m["scale"], m["height"] / m["scale"]
    return (h, w) if m.get("transform", 0) % 2 == 1 else (w, h)


def _raw_monitors():
    return [m for m in _hyprctl_json("monitors") if not m.get("disabled")]


def monitors(bus=None):
    """Active monitors. The focused one counts as primary (Hyprland has no primary monitor)."""
    out = []
    for m in _raw_monitors():
        w, h = m["width"], m["height"]
        if m.get("transform", 0) % 2 == 1:
            w, h = h, w
        name = " ".join(x for x in (m.get("make"), m.get("model")) if x).strip() or m["name"]
        out.append(Monitor(m["name"], name, w, h, float(m.get("refreshRate") or 60), bool(m.get("focused")),
                           m["x"], m["y"], float(m.get("scale") or 1.0)))
    out.sort(key=lambda m: (not m.primary, m.x, m.y))
    return out


def _watch_events(names, callback, delay_ms):
    """Calls callback() on the main thread, delay_ms after Hyprland sends one of the events in
    names (events that come close together make one call)."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "hypr", sig, ".socket2.sock")
    pending = threading.Event()

    def fire():
        pending.clear()
        callback()
        return False

    def loop():
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(path)
                for line in s.makefile("r", encoding="utf-8", errors="replace"):
                    if line.split(">>", 1)[0] in names and not pending.is_set():
                        pending.set()
                        GLib.timeout_add(delay_ms, fire)
            except OSError as e:
                log.debug("event socket: %s", e)
            time.sleep(5)
    threading.Thread(target=loop, name="hypr-events", daemon=True).start()


def watch_monitors(bus, callback):
    """Calls callback() on the main thread when a monitor comes or goes."""
    _watch_events({"monitoradded", "monitorremoved"}, callback, 500)


def watch_windows(callback):
    """Calls callback() on the main thread when a window or workspace comes, goes, moves or is retitled."""
    _watch_events({"openwindow", "closewindow", "movewindow", "movewindowv2", "windowtitle", "windowtitlev2",
                   "createworkspace", "createworkspacev2", "destroyworkspace", "destroyworkspacev2",
                   "moveworkspace", "moveworkspacev2", "renameworkspace"}, callback, 300)


def _source_id(kind, key):
    return (zlib.crc32(f"{kind}:{key}".encode()) & 0x7FFFFFFF) | 0x100


def sources():
    """The windows and the workspaces that have windows, for the viewer's list. Each is a dict of
    the wire fields (id, title, appName, bundleID, width, height) and, with a leading underscore,
    what the host needs: _icon (for icons.icon_png), _monitor (its connector), _focus (what
    focus() shows: "workspace" or "window" and the argument) and, for thumbs.py, _ws (the
    workspace's id); for a workspace _canvas (its monitor's size), for a window _stable (its
    stableId), _px (its size in pixels), _rect (where it is on its monitor), _rank (0 is the
    window focused last), _address (its hyprctl address) and _geo (where it is on the layout, in
    the layout's logical pixels: x, y, width, height)."""
    monitors = {m["id"]: m for m in _raw_monitors()}
    clients = [c for c in _hyprctl_json("clients") if c.get("mapped") and not c.get("hidden", False)]
    clients.sort(key=lambda c: (c["workspace"]["id"], c["at"][1], c["at"][0]))
    workspaces = []
    for ws in _hyprctl_json("workspaces"):
        normal = ws["type"] == "normal" if "type" in ws else ws["id"] > 0     # not a special workspace
        if normal and ws["windows"] > 0 and ws["monitorID"] in monitors:
            workspaces.append(ws)
    workspaces.sort(key=lambda ws: ws["id"])
    out = []
    for ws in workspaces:
        m = monitors[ws["monitorID"]]
        w, h = _logical_size(m)
        classes = []
        for c in clients:
            if c["workspace"]["id"] == ws["id"] and c["class"] and c["class"] not in classes:
                classes.append(c["class"])
        ref = str(ws["id"]) if ws["name"] == str(ws["id"]) else f"name:{ws['name']}"
        out.append({"id": _source_id("workspace", ws["id"]), "title": f"Workspace {ws['name']}: {', '.join(classes)}",
                    "appName": "Workspace", "bundleID": f"linux.workspace.{ws['name']}", "width": int(w),
                    "height": int(h), "_icon": ("workspace", ws["name"]), "_ws": ws["id"], "_canvas": (w, h),
                    "_monitor": m["name"], "_focus": ("workspace", ref)})
    for c in clients:
        m = monitors.get(c["monitor"])
        if m is None:
            continue    # a window on no monitor (a special workspace that is hidden)
        scale = float(m.get("scale") or 1.0)
        out.append({"id": _source_id("window", c["address"]), "title": c["title"] or c["class"],
                    "appName": c["class"] or "Window", "bundleID": f"linux.window.{c['class']}",
                    "width": int(c["size"][0] / scale), "height": int(c["size"][1] / scale),
                    "_icon": ("app", c["class"]), "_monitor": m["name"], "_focus": ("window", f"address:{c['address']}")}
                   | {"_address": c["address"], "_geo": (c["at"][0], c["at"][1], c["size"][0], c["size"][1])}
                   | ({"_stable": c["stableId"], "_px": (c["size"][0] * scale, c["size"][1] * scale),
                       "_ws": c["workspace"]["id"], "_rank": c.get("focusHistoryID", 0),
                       "_rect": (c["at"][0] - m["x"], c["at"][1] - m["y"], c["size"][0], c["size"][1])}
                      if c.get("stableId") else {}))
    return out


def _dispatch(*args):
    r = subprocess.run(["hyprctl", "dispatch", *args], capture_output=True, text=True, timeout=5)
    return r.returncode == 0 and r.stdout.strip() == "ok", (r.stdout or r.stderr).strip()


def focus(target):
    """Shows a window or a workspace from sources(): target is its _focus."""
    kind, ref = target
    # Hyprland 0.55 and later dispatch Lua (hl.dsp.focus); earlier ones take the dispatcher's name.
    ok, said = _dispatch(f"hl.dsp.focus({{ {kind} = {json.dumps(ref, ensure_ascii=False)} }})")
    if not ok and "hl.dispatch" in said:
        ok, said = _dispatch(*(("workspace", ref) if kind == "workspace" else ("focuswindow", ref)))
    if not ok:
        log.warning("could not show %s %s: %s", kind, ref, said)


def portal_available(bus):
    try:
        bus.call_sync(PORTAL_BUS, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (SC_IFACE, "version")), None, Gio.DBusCallFlags.NONE, 5000, None)
        return True
    except GLib.Error:
        return False


def picker_configured():
    """Whether xdph's config names sill-linux-picker as its picker."""
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "hypr", "xdph.conf")
    try:
        with open(path) as f:
            return "sill-linux-picker" in f.read()
    except OSError:
        return False


def pick_path():
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/sill-linux-{os.getuid()}"
    return os.path.join(base, "sill-linux-pick")


class _Tokens:
    """The portal's restore tokens, one per monitor: with one, the portal skips its picker."""

    def __init__(self):
        self.path = os.path.join(config_dir(), "portal-tokens.json")

    def get(self, connector):
        try:
            with open(self.path) as f:
                return json.load(f).get(connector)
        except (OSError, ValueError):
            return None

    def put(self, connector, token):
        try:
            with open(self.path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        saved[connector] = token
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(saved, f)
        os.replace(tmp, self.path)


# US layout: the character each key (and Shift) types, for text the device sends as characters.
_US_PLAIN = dict(zip("1234567890-=qwertyuiop[]asdfghjkl;'`\\zxcvbnm,./ ",
                     [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
                      30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 57]))
_US_SHIFT = dict(zip("!@#$%^&*()_+QWERTYUIOP{}ASDFGHJKL:\"~|ZXCVBNM<>?",
                     [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
                      30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53]))
KEY_LEFTSHIFT = 42


class Input:
    """A virtual mouse and keyboard (uinput). Absolute pointer positions span every monitor, as
    Hyprland maps an absolute device to the whole layout."""

    def __init__(self):
        from evdev import AbsInfo, UInput, ecodes as e
        self.e = e
        self.lock = threading.Lock()
        self.kbd = UInput({e.EV_KEY: list(range(1, 249))}, name="Sill virtual keyboard")
        ax = AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)
        self.ptr = UInput({e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
                           e.EV_ABS: [(e.ABS_X, ax), (e.ABS_Y, ax)],
                           e.EV_REL: [e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES]},
                          name="Sill virtual pointer")
        self.wheel = [0.0, 0.0]          # hi-res units not yet sent as whole wheel clicks

    def close(self):
        for d in (self.kbd, self.ptr):
            try:
                d.close()
            except OSError:
                pass

    def move(self, ax, ay):
        e = self.e
        with self.lock:
            self.ptr.write(e.EV_ABS, e.ABS_X, max(0, min(ABS_MAX, int(round(ax)))))
            self.ptr.write(e.EV_ABS, e.ABS_Y, max(0, min(ABS_MAX, int(round(ay)))))
            self.ptr.syn()

    def button(self, button, pressed):
        with self.lock:
            self.ptr.write(self.e.EV_KEY, button, 1 if pressed else 0)
            self.ptr.syn()

    def scroll(self, dx, dy):
        """dx, dy in GNOME's scroll units (10 per wheel click), positive down and right."""
        e = self.e
        hx, hy = dx * 12.0, -dy * 12.0       # 120 hi-res units per click; the wheel's up is positive
        with self.lock:
            for i, (code, hi, v) in enumerate(((e.REL_HWHEEL, e.REL_HWHEEL_HI_RES, hx),
                                               (e.REL_WHEEL, e.REL_WHEEL_HI_RES, hy))):
                if not v:
                    continue
                self.ptr.write(e.EV_REL, hi, int(round(v)))
                self.wheel[i] += v
                clicks = int(self.wheel[i] / 120)
                if clicks:
                    self.wheel[i] -= clicks * 120
                    self.ptr.write(e.EV_REL, code, clicks)
            self.ptr.syn()

    def key(self, code, pressed):
        with self.lock:
            self.kbd.write(self.e.EV_KEY, code, 1 if pressed else 0)
            self.kbd.syn()

    def char(self, ch):
        """Types one character, on a US layout. False when it has no key."""
        shift = ch in _US_SHIFT
        code = _US_SHIFT.get(ch) or _US_PLAIN.get(ch)
        if code is None:
            return False
        if shift:
            self.key(KEY_LEFTSHIFT, True)
        self.key(code, True)
        self.key(code, False)
        if shift:
            self.key(KEY_LEFTSHIFT, False)
        return True


class Capture:
    """One portal ScreenCast session of one monitor, plus input and clipboard. Call from the GLib
    main thread; on_ready(node_id) runs once the PipeWire stream exists, on_closed() if it ends.
    `pipewire_fd` is the portal's PipeWire connection, for pipewiresrc fd=."""

    def __init__(self, bus, monitor=None, virtual=False, cursor_mode=CURSOR_HIDDEN, on_ready=None, on_closed=None,
                 window=None):
        if virtual:
            raise GLib.Error("virtual screens need GNOME")
        self.bus = bus
        self.connector = monitor        # the monitor the stream is of, or the one the window is on
        self.window = window            # a window's hyprctl address: the stream is of that window alone
        self.on_ready, self.on_closed = on_ready, on_closed
        self.closed = False
        self.subs = []
        self.session = None
        self.pipewire_fd = None
        self.clip_proc = None
        self.tokens = _Tokens()
        try:
            self.input = Input()
        except (OSError, ImportError) as e:
            raise GLib.Error(f"no virtual mouse and keyboard (/dev/uinput: {e}); "
                             "add yourself to the input group, or run install.sh") from e
        self.sender = bus.get_unique_name()[1:].replace(".", "_")
        self.cursor = PORTAL_CURSOR.get(cursor_mode, 1)
        self._request("CreateSession", lambda token: GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", "sill" + token)},)), self._created)
        log.info("screen cast requested (%s)", monitor)

    # --- the portal's request and response ------------------------------------------------------
    def _request(self, method, params, on_response):
        token = f"sill{os.getpid()}_{int(time.monotonic() * 1000) % 10**9}_{len(self.subs)}"
        path = f"{PORTAL_PATH}/request/{self.sender}/{token}"
        sub = None

        def response(_c, _s, _p, _i, _sig, args):
            self.bus.signal_unsubscribe(sub)
            if sub in self.subs:
                self.subs.remove(sub)
            code, results = args.unpack()
            if self.closed:
                return
            if code != 0:
                self._fail(f"{method}: {'cancelled' if code == 1 else 'refused'} by the portal")
                return
            try:
                on_response(results)
            except (GLib.Error, OSError, ValueError, KeyError) as e:
                self._fail(f"{method}: {getattr(e, 'message', e)}")
        sub = self.bus.signal_subscribe(PORTAL_BUS, REQUEST, "Response", path, None, Gio.DBusSignalFlags.NONE, response)
        self.subs.append(sub)
        self.bus.call_sync(PORTAL_BUS, PORTAL_PATH, SC_IFACE, method, params(token), None,
                           Gio.DBusCallFlags.NONE, 10000, None)

    def _created(self, results):
        self.session = results["session_handle"]
        self.subs.append(self.bus.signal_subscribe(PORTAL_BUS, SESSION, "Closed", self.session, None,
                                                   Gio.DBusSignalFlags.NONE, lambda *a: self._fail("session closed")))
        # A window gets no restore token: its handle is gone with the window.
        opts = {"types": GLib.Variant("u", SOURCE_MONITOR | SOURCE_WINDOW if self.window else SOURCE_MONITOR),
                "multiple": GLib.Variant("b", False), "cursor_mode": GLib.Variant("u", self.cursor),
                "persist_mode": GLib.Variant("u", 0 if self.window else 2)}
        token = None if self.window else self.tokens.get(self.connector)
        if token:
            opts["restore_token"] = GLib.Variant("s", token)
        # sill-linux-picker reads this, so xdph's picker answers without asking anyone.
        try:
            with open(pick_path(), "w") as f:
                f.write(f"window:{self.window}" if self.window else f"screen:{self.connector or ''}")
        except OSError as e:
            log.debug("pick file: %s", e)
        self._request("SelectSources", lambda t: GLib.Variant("(oa{sv})", (self.session, dict(
            opts, handle_token=GLib.Variant("s", t)))), self._selected)

    def _selected(self, _results):
        self._request("Start", lambda t: GLib.Variant("(osa{sv})", (self.session, "", {
            "handle_token": GLib.Variant("s", t)})), self._started)

    def _started(self, results):
        try:
            os.unlink(pick_path())
        except OSError:
            pass
        if results.get("restore_token") and not self.window:
            self.tokens.put(self.connector, results["restore_token"])
        streams = results.get("streams") or []
        if not streams:
            raise ValueError("no stream")
        node = streams[0][0]
        r, fds = self.bus.call_with_unix_fd_list_sync(
            PORTAL_BUS, PORTAL_PATH, SC_IFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session, {})), GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE, 5000, None, None)
        self.pipewire_fd = fds.get(r.unpack()[0])
        log.info("PipeWire node %d (%s)", node, self.connector)
        if self.on_ready and not self.closed:
            self.on_ready(node)

    def _fail(self, why):
        if self.closed:
            return
        log.warning("screen cast ended: %s", why)
        self.stop()
        if self.on_closed:
            self.on_closed()

    def stop(self):
        if self.closed:
            return
        self.closed = True
        for s in self.subs:
            self.bus.signal_unsubscribe(s)
        self.subs = []
        if self.session:
            try:
                self.bus.call_sync(PORTAL_BUS, self.session, SESSION, "Close", None, None,
                                   Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error as e:
                log.debug("close: %s", e.message)
        if self.clip_proc is not None:
            self.clip_proc.terminate()
            self.clip_proc = None
        if self.pipewire_fd is not None:      # pipewiresrc keeps its own copy of it
            os.close(self.pipewire_fd)
            self.pipewire_fd = None
        self.input.close()

    # --- clipboard (wl-clipboard) -------------------------------------------------------------------
    def enable_clipboard(self, on_text, limit):
        if not (shutil.which("wl-paste") and shutil.which("wl-copy")):
            log.warning("clipboard sharing is off: install wl-clipboard")
            return False
        self.on_clip, self.clip_limit = on_text, limit
        try:
            # One line per copy on this computer (wl-paste runs `echo` each time).
            self.clip_proc = subprocess.Popen(["wl-paste", "--watch", "echo"], stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError as e:
            log.warning("clipboard sharing is off: %s", e)
            return False
        threading.Thread(target=self._watch_clipboard, args=(self.clip_proc,), daemon=True).start()
        return True

    def _watch_clipboard(self, proc):
        first = True
        for _line in proc.stdout:
            if first:     # wl-paste --watch also fires for what was copied before
                first = False
                continue
            if self.closed:
                return
            types = self._paste("--list-types") or b""
            mime = next((m for m in TEXT_MIMES if m in types.decode("utf-8", "replace").split()), None)
            if mime is None:
                continue
            data = self._paste("--type", mime)
            if data is None or len(data) > self.clip_limit:
                continue
            text = data.decode("utf-8", "replace")
            if text and self.on_clip:
                self.on_clip(text)

    def _paste(self, *args):
        try:
            r = subprocess.run(["wl-paste", "--no-newline", *args], capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    def set_text(self, text):
        if self.closed or not shutil.which("wl-copy"):
            return
        try:
            subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8"], input=text.encode("utf-8"), timeout=3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("clipboard write: %s", e)

    def copied_paths(self):
        from urllib.parse import unquote, urlparse
        if self.closed:
            return []
        data = self._paste("--type", "text/uri-list") or b""
        paths = []
        for line in data.decode("utf-8", "replace").splitlines():
            u = urlparse(line.strip())
            if u.scheme == "file":
                paths.append(unquote(u.path))
        log.info("copied files: %d", len(paths))
        return paths

    # --- input (any thread) ----------------------------------------------------------------------
    def motion(self, x, y):
        """x, y in the stream's pixels."""
        if self.closed:
            return
        geo = getattr(self, "geometry", None)
        now = time.monotonic()
        if geo is None or now - geo[0] > 2:
            try:
                mons = _raw_monitors()
            except (OSError, ValueError, subprocess.SubprocessError):
                return
            geo = self.geometry = (now, mons)
        mons = geo[1]
        m = next((m for m in mons if m["name"] == self.connector), mons[0] if mons else None)
        if m is None:
            return
        lw, lh = _logical_size(m)
        pw, ph = (m["height"], m["width"]) if m.get("transform", 0) % 2 == 1 else (m["width"], m["height"])
        self._move_to(mons, m["x"] + x / max(pw, 1) * lw, m["y"] + y / max(ph, 1) * lh)

    def motion_global(self, gx, gy):
        """gx, gy on the layout, in its logical pixels (a window's position is in those)."""
        if self.closed:
            return
        geo = getattr(self, "geometry", None)
        if geo is None or time.monotonic() - geo[0] > 2:
            try:
                geo = self.geometry = (time.monotonic(), _raw_monitors())
            except (OSError, ValueError, subprocess.SubprocessError):
                return
        self._move_to(geo[1], gx, gy)

    def _move_to(self, mons, gx, gy):
        boxes = [(o["x"], o["y"], *_logical_size(o)) for o in mons]
        left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
        right, bottom = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
        self.input.move((gx - left) / max(right - left, 1) * ABS_MAX, (gy - top) / max(bottom - top, 1) * ABS_MAX)

    def button(self, button, pressed):
        if not self.closed:
            self.input.button(button, pressed)

    def axis(self, dx, dy, flags=0):
        if not self.closed and (dx or dy):
            self.input.scroll(dx, dy)

    def key(self, keycode, pressed):
        if not self.closed:
            self.input.key(keycode, pressed)

    def keysym(self, keysym, pressed):
        """Only the press types (a character's key goes down and up at once)."""
        if self.closed or not pressed:
            return
        ch = chr(keysym if keysym < 0x01000000 else keysym - 0x01000000)
        if not self.input.char(ch):
            log.debug("no key types U+%04X on a US layout", ord(ch))
