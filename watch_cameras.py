#!/usr/bin/env python3
"""
watch_cameras.py -- JSON-driven low-latency multi-pane ffplay camera wall.

Concept
-------
This ffplay build accepts exactly ONE input per process (its option parser
rejects a second "-i"), so split views are built from several borderless
ffplay windows placed with "-left/-top":

    fullscreen window = 1 ffplay, output scaled to the whole monitor
    2x2 split         = 4 ffplay tiles, each output letterboxed to its rect

One process per pane means one dead camera only kills its own tile, and the
supervisor restarts that tile (with backoff) without touching the others.
A monitor that isn't connected doesn't stop the wall: its panes are held and
are launched automatically as soon as it reappears (xrandr is re-polled every
few seconds, re-tiling its panes if the monitor comes back at a new size).
Borderless windows are positioned with ffplay's own -left/-top; placement is
checked after boot via xprop/xwininfo (xdotool is used only if present, to
fix a WM that shifted a window).

Usage
-----
  python3 watch_cameras.py --print-monitors         # live display geometry
  python3 watch_cameras.py -c walls.json --validate
  python3 watch_cameras.py -c walls.json --preview  # print exact ffplay argv
  python3 watch_cameras.py -c walls.json            # run the wall
  python3 watch_cameras.py -c walls.json -v         # verbose ffplay logs

Ctrl-C stops everything. Per-pane logs: ./logs/<window>__<pane>.log

Configs
-------
Monitors need only an "id": x/y/width/height are auto-detected from xrandr.
Explicit geometry in the config overrides the live reading (handy for
--validate on headless boxes).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

log = logging.getLogger("watch_cameras")

RTSP_SCHEMES = ("rtsp", "rtsps")
MASK_RE = re.compile(r"://([^:/@?]+):([^@/?]+)@")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def mask_url(url: str) -> str:
    return MASK_RE.sub(r"://\1:***@", url)


def mask_args(args: list[str]) -> list[str]:
    """Mask credentials in any token (rtsp user:pass@...)."""
    return [MASK_RE.sub(r"://\1:***@", a) if MASK_RE.search(a) else a for a in args]


# ---------------------------------------------------------------- config

def load_config(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        cfg = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"bad JSON in {path}: {e}")
    for key in ("cameras", "monitors", "windows", "latency", "placement", "env"):
        cfg.setdefault(key, [] if key in ("cameras", "monitors", "windows") else {})
    cfg["__path"] = str(p)
    cfg["__cwd"] = str(p.parent)
    return cfg


def build_url(cam: dict) -> str:
    url = str(cam["url"]).strip()
    user, pw = cam.get("username"), cam.get("password")
    u = urlparse(url)
    if (user or pw) and not u.username:
        # percent-encode: a raw '?' (or ':', '@', ...) in the password would
        # be parsed as URL structure and mangle the host/port.
        cred = f"{quote(str(user or ''), safe='')}:{quote(str(pw or ''), safe='')}"
        m = re.match(r"^([a-z+][a-z0-9+.-]*)://", url)
        if m:
            url = f"{m.group(1)}://{cred}@" + url[m.end():]
    return url


def discover_monitors() -> list[dict]:
    mons: list[dict] = []
    out = _x(["xrandr", "--listmonitors"])
    if out:
        for line in out.splitlines():
            m = re.search(r"(\d+):\s+\+?(\S+)\s+(\d+)/\d+x(\d+)/\d+\s+'?(\d+)\+(-?\d+)", line)
            if m and m.group(1) != "0":
                mons.append({"id": m.group(2), "width": int(m.group(3)),
                             "height": int(m.group(4)), "x": int(m.group(5)),
                             "y": int(m.group(6))})
    seen = {m["id"] for m in mons}
    q = _x(["xrandr", "-q"]) or ""
    for m in re.finditer(
            r"^(\S+)\s+connected\b.*?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", q, re.M):
        if m.group(1) in seen:
            continue
        mons.append({"id": m.group(1), "width": int(m.group(2)),
                     "height": int(m.group(3)), "x": int(m.group(4)),
                     "y": int(m.group(5))})
    return mons


# placeholder geometry for a configured monitor that xrandr doesn't report and
# has no explicit size; keeps tile math working while its panes wait for it
def _def_geom() -> dict:
    return {"x": 0, "y": 0, "width": 1920, "height": 1080}


def resolve_monitors(cfg: dict) -> list[dict]:
    """Resolve each configured monitor to a geometry.

    An entry may be just {"id": "rdp-0"}; omitted x/y/width/height are read
    from the live display. Explicit values always win (offline / reproducible).
    A monitor xrandr does not report is marked connected: False -- its panes
    are held until it appears -- and gets placeholder geometry unless the
    config pins a size.
    """
    live = {m["id"]: m for m in discover_monitors()}
    out: list[dict] = []
    for m in cfg["monitors"]:
        mid = m.get("id")
        lm = live.get(mid)
        merged = dict(m)
        pinned = m.get("width") is not None and m.get("height") is not None
        for k in ("x", "y", "width", "height"):
            if merged.get(k) is None:
                merged[k] = (lm or _def_geom())[k]
        if lm is None:
            log.warning("monitor '%s' not connected -- panes on it are held until it "
                        "reappears (tiling with %s %dx%d geometry)",
                        mid, "configured" if pinned else "placeholder", merged["width"],
                        merged["height"])
        merged["id"] = mid
        merged["connected"] = lm is not None
        out.append(merged)
    return out


def compute_tiles(win: dict, mon: dict) -> list[list[int]]:
    if win.get("layout", "full") == "full":
        return [[int(mon["x"]), int(mon["y"]), int(mon["width"]), int(mon["height"])]]
    rows, cols = int(win.get("rows", 2)), int(win.get("cols", 2))
    if rows < 1 or cols < 1:
        raise ConfigError(f"window '{win['id']}': rows/cols must be >= 1")
    gap = int(win.get("gap", 8))
    if (cols + 1) * gap >= mon["width"] or (rows + 1) * gap >= mon["height"]:
        raise ConfigError(f"window '{win['id']}': gap {gap}px too large for monitor")
    mw, mh = mon["width"], mon["height"]
    ux, uy = mw - (cols + 1) * gap, mh - (rows + 1) * gap
    tw, th = ux // cols, uy // rows
    tiles: list[list[int]] = []
    ox, oy = int(mon["x"]), int(mon["y"])
    right, bottom = ox + mw - gap, oy + mh - gap
    xs = [ox + gap + c * (tw + gap) for c in range(cols)]
    ys = [oy + gap + r * (th + gap) for r in range(rows)]

    # stretch last row/col to consume rounding remainder
    for i in range(cols * rows):
        r, c = divmod(i, cols)
        x0, y0 = xs[c], ys[r]
        x1 = xs[c + 1] - gap if c < cols - 1 else right
        y1 = ys[r + 1] - gap if r < rows - 1 else bottom
        tiles.append([x0, y0, x1 - x0, y1 - y0])
    # floor to even: scale=…force_original_aspect_ratio=decrease rounds its fit
    # to codec-compatible dimensions, so a 1065px-tall tile can receive a 1066px
    # frame and pad() then rejects it ("Padded dimensions cannot be smaller…").
    tiles = [[x, y, w // 2 * 2, h // 2 * 2] for (x, y, w, h) in tiles]
    return tiles


def validate_config(cfg: dict) -> list[dict]:
    """Flatten config into pane descriptors; raise ConfigError on problems."""
    cam_map = {c["id"]: c for c in cfg["cameras"]}
    monitors = resolve_monitors(cfg)
    mon_map = {m["id"]: m for m in monitors}
    for c in cfg["cameras"]:
        if not c.get("url"):
            raise ConfigError(f"camera '{c['id']}' has no 'url'")
    for w in cfg["windows"]:
        for k in ("id", "monitor"):
            if not w.get(k):
                raise ConfigError(f"window has no '{k}': {w!r}")
        if w.get("layout", "full") not in ("full", "split"):
            raise ConfigError(f"window '{w['id']}': layout must be full|split")

    out: list[dict] = []
    titles: set[str] = set()
    for w in cfg["windows"]:
        mon = mon_map[w["monitor"]]
        tiles = compute_tiles(w, mon)
        panes = list(w.get("panes", []))
        if w.get("layout", "full") == "full":
            if len(panes) != 1:
                raise ConfigError(f"window '{w['id']}': full layout needs exactly 1 pane")
        else:
            need = int(w.get("rows", 2)) * int(w.get("cols", 2))
            if len(panes) > need:
                raise ConfigError(f"window '{w['id']}': too many panes ({len(panes) > need})")
            while len(panes) < need:
                panes.append({"blank": True})

        for i, pane in enumerate(panes):
            rect = tiles[i]
            x, y, tw, th = rect
            if tw < 16 or th < 16:
                raise ConfigError(f"window '{w['id']}' pane {i}: tile too small {tw}x{th}")
            pid = pane.get("id") or w["id"] if len(panes) == 1 else pane.get("id") or f"p{i+1}"
            title = f"{w['id']}::{pid}"[:63]
            if title in titles:
                raise ConfigError(f"duplicate pane id -> colliding window title '{title}'")
            titles.add(title)

            cam = None
            if not pane.get("blank"):
                cid = pane.get("camera")
                cam = cam_map.get(cid)
                if cam is None:
                    raise ConfigError(f"window '{w['id']}' pane '{pid}': "
                                      f"unknown camera '{cid}'")
            args, url = build_ffplay_args(cfg, w, cam, title, x, y, tw, th,
                                          split=w.get("layout", "full") == "split")
            out.append({"window_id": w["id"], "pane_id": pid, "title": title,
                        "monitor": mon["id"],
                        "monitor_connected": bool(mon.get("connected", True)),
                        "x": x, "y": y, "w": tw, "h": th,
                        "camera": cam["id"] if cam else None,
                        "url": mask_url(url), "args": args,
                        "restart": pane.get("restart", True),
                        "ffplay": cfg.get("ffplay", "ffplay")})
    return out


def build_ffplay_args(cfg: dict, win: dict, cam: dict | None, title: str,
                      x: int, y: int, w: int, h: int,
                      split: bool = False) -> tuple[list[str], str]:
    if cam is None:
        url = f"color=black:size={w}x{h}:rate=15"
    else:
        url = build_url(cam)
    scheme = urlparse(url).scheme.lower()

    args: list[str] = [str(cfg.get("ffplay", "ffplay")), "-hide_banner"]
    args += ["-loglevel", "verbose" if cfg.get("verbose") else "info"]
    args += ["-sn", "-an", "-nostats", "-noborder"]
    if cfg["placement"].get("override_redirect"):
        args += ["-n"]
    args += ["-left", str(int(x)), "-top", str(int(y))]
    if cfg["placement"].get("alwaysontop"):
        args += ["-alwaysontop"]
    args += ["-window_title", title]
    args += [str(a) for a in (win.get("global_extra") or [])]

    if cam is not None and scheme in RTSP_SCHEMES:
        args += rtsp_latency_args(cfg, cam)
        url += rtsp_url_options(cfg, cam)
    if cam is not None and not scheme.startswith(("rtsp",)) and "://" not in url:
        args += ["-f", "lavfi"]
    if cam is not None:
        args += [str(a) for a in (cam.get("input_extra") or [])]
    args += ["-i", url]

    vf: list[str] = []
    # Fit the camera into the tile, letterbox (no stretch), square pixels.
    # Tile dims are even (see compute_tiles) so the scaled fit always fits in
    # pad's target.
    vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos")
    vf.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    vf.append("setsar=1")
    if cfg.get("labels", True) and cam is not None:
        lf = label_file(cfg, title, cam.get("label", cam["id"]))
        vf.append(
            f"drawtext=fontfile={font_path()}:textfile={lf}:fontsize={max(18, h // 20)}:"
            "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=10:x=20:y=20")
    args += ["-vf", ",".join(vf)]
    return args, url


def rtsp_latency_args(cfg: dict, cam: dict) -> list[str]:
    """Low-latency global options (must come before -i), plus per-pane tweaks."""
    ll = dict(cfg.get("latency") or {})
    if isinstance(cam.get("latency"), dict):
        ll.update({k: v for k, v in cam["latency"].items() if v is not None})

    a: list[str] = []
    a += ["-rtsp_transport", str(ll.get("rtsp_transport", "tcp"))]
    # fast connect: small TCP connect timeout (microseconds for ffmpeg's -timeout)
    a += ["-timeout", str(int(ll.get("connect_timeout_ms", 1000) * 1000))]
    # decoder / demuxer: no buffering, discard anything damaged on arrival
    a += ["-fflags", "+genpts+discardcorrupt+nobuffer"]
    a += ["-flags", "low_delay"]
    if ll.get("wallclock_ts", True):
        a += ["-use_wallclock_as_timestamps", "1"]
    # skip network probing (no IDR-waiting) at stream start
    a += ["-probesize", str(int(ll.get("probesize", 32768)))]
    a += ["-analyzeduration", str(int(ll.get("analyzeduration", 0)))]
    # slice threading: decode one whole frame in parallel -> lowest per-frame CPU time
    a += ["-thread_type", str(ll.get("thread_type", "slice"))]
    if ll.get("framedrop", True):
        a += ["-framedrop", "-infbuf", "-max_delay", "0"]
    # in-stream reconnect (ffplay experimental): ride out brief network glitches
    if ll.get("reconnect_delay_max_s") and int(ll.get("reconnect_delay_max_s", 0)) > 0:
        a += ["-reconnect", "1", "-reconnect_streamed", "1",
              "-reconnect_delay_max", str(int(ll["reconnect_delay_max_s"]))]
    devs = ll.get("hardware_decoders") or []
    for d in devs:
        a += ["-hwaccel", str(d)]
    return a


def rtsp_url_options(cfg: dict, cam: dict) -> str:
    """RTSP timeout knobs have to ride as URL query params (stimeout & co)."""
    ll = dict(cfg.get("latency") or {})
    if isinstance(cam.get("latency"), dict):
        ll.update({k: v for k, v in cam["latency"].items() if v is not None})
    q: list[str] = []
    if ll.get("io_timeout_ms") is not None:
        q.append(f"stimeout={int(ll['io_timeout_ms'] * 1000)}")
    if ll.get("keepalive", False):
        q.append("rtsp_flags=keepalive")
    if not q:
        return ""
    sep = "&" if "?" in str(cam.get("url", "")) else "?"
    return sep + "&".join(q)


def font_path() -> str:
    for c in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"):
        if os.path.exists(c):
            return c
    return ""


def label_file(cfg: dict, title: str, text: str) -> str:
    d = Path(cfg.get("log_dir", "logs")) / "labels"
    try:
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", title)
        p = d / f"{safe}.txt"
        p.write_text(text)
        return str(p)
    except OSError:
        return ""


# ---------------------------------------------------------------- X11 helpers

def _x(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def x11_client_windows() -> list[str]:
    out = _x(["xprop", "-root", "_NET_CLIENT_LIST"])
    return re.findall(r"0x[0-9a-fA-F]+", out) if out else []


def x11_geom(wid: str) -> tuple[int, int, int, int] | None:
    out = _x(["xwininfo", "-id", wid])
    if not out:
        return None
    gs = {}
    for pat, k in ((r"Absolute upper-left X:\s+(-?\d+)", "x"),
                  (r"Absolute upper-left Y:\s+(-?\d+)", "y"),
                  (r"\bWidth:\s+(\d+)", "w"), (r"\bHeight:\s+(\d+)", "h")):
        m = re.search(pat, out)
        if not m:
            return None
        gs[k] = int(m.group(1))
    return gs["x"], gs["y"], gs["w"], gs["h"]


def x11_find(title: str) -> str | None:
    for wid in x11_client_windows():
        out = _x(["xprop", "-id", wid, "_NET_WM_NAME", "WM_NAME"])
        if out and title in out:
            return wid
    return None


def x11_move(wid: str, x: int, y: int) -> bool:
    if not shutil.which("xdotool"):
        return False
    try:
        r = subprocess.run(["xdotool", "windowmove", "--sync", wid, str(x), str(y)],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- supervisor


def _panes_region(group: list) -> tuple[int, int, int, int]:
    """Bounding box of a group of panes (the tiles of one monitor)."""
    minx = min(p.d["x"] for p in group)
    miny = min(p.d["y"] for p in group)
    maxx = max(p.d["x"] + p.d["w"] for p in group)
    maxy = max(p.d["y"] + p.d["h"] for p in group)
    return minx, miny, maxx - minx, maxy - miny


def _retile_monitor(cfg: dict, mon: dict, group: list, mid: str) -> bool:
    """Rebuild tile rects + ffplay argv for a monitor's panes at new geometry.
    Updates each Pane in place. Returns False if any window couldn't re-tile.
    """
    cams = {c["id"]: c for c in cfg["cameras"]}
    by_win: dict[str, list] = {}
    for p in group:
        by_win.setdefault(p.d["window_id"], []).append(p)
    ok = True
    for wid, plist in by_win.items():
        win = next((w for w in cfg["windows"] if w["id"] == wid), None)
        if win is None:
            continue
        try:
            tiles = compute_tiles(win, mon)
        except ConfigError as e:
            log.error("window %s on monitor %s: cannot re-tile (%s); keeping old geometry",
                      wid, mid, e)
            ok = False
            continue
        for i, p in enumerate(plist):
            x, y, tw, th = tiles[i]
            args, _ = build_ffplay_args(cfg, win, cams.get(p.d["camera"]), p.d["title"],
                                        x, y, tw, th,
                                        split=win.get("layout", "full") == "split")
            p.d.update({"x": x, "y": y, "w": tw, "h": th, "args": args})
            p.placed = ""
    return ok


def _poll_monitors(cfg: dict, mon_conn: dict, mon_panes: dict) -> None:
    """Re-check xrandr. Stop the panes whose monitor vanished; when a monitor
    reappears, re-tile its panes (unless its config entry pins geometry) and
    start them again."""
    if _x(["xrandr", "--listmonitors"]) is None:
        return  # xrandr itself failed this round; keep the previous state
    live = {m["id"]: m for m in discover_monitors()}
    entries = {str(m.get("id")): m for m in cfg["monitors"]}
    for mid, group in mon_panes.items():
        if not group:
            continue
        if (mid in live) == mon_conn.get(mid, mid in live):
            continue
        mon_conn[mid] = mid in live
        if not (mid in live):
            log.warning("monitor %s disconnected -- stopping %d pane(s); "
                        "they will restart when it reconnects", mid, len(group))
            for p in group:
                p.monitor_down = True
                p.stop()
        else:
            lm = live[mid]
            entry = entries.get(mid) or {}
            pinned = any(entry.get(k) is not None for k in ("x", "y", "width", "height"))
            if not pinned:
                geo = _panes_region(group)
                if (lm["x"], lm["y"], lm["width"], lm["height"]) != geo:
                    mon = {k: lm[k] for k in ("x", "y", "width", "height")}
                    if _retile_monitor(cfg, mon, group, mid):
                        log.info("monitor %s is now at %d,%d %dx%d (was %d,%d %dx%d) "
                                 "-- re-tiled its panes",
                                 mid, lm["x"], lm["y"], lm["width"], lm["height"],
                                 geo[0], geo[1], geo[2], geo[3])
            log.info("monitor %s connected -- starting %d pane(s)", mid, len(group))
            now = time.monotonic()
            for p in group:
                p.monitor_down = False
                p.fails = 0
                p.placed = ""
                p.next_start = now


class Pane:
    def __init__(self, desc: dict, cfg: dict):
        self.d = desc
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.fails = 0
        self.next_start = 0.0
        self.started_at = 0.0
        self.monitor_down = False   # its monitor is absent: don't spawn ffplay
        self.placed: str = ""
        self.logpath = Path(cfg.get("log_dir", "logs")) / \
            (re.sub(r"[^A-Za-z0-9._-]+", "_", f"{desc['window_id']}__{desc['pane_id']}") + ".log")
        self.logpath.parent.mkdir(parents=True, exist_ok=True)

    def start(self, env: dict) -> None:
        st = self.logpath.stat() if self.logpath.exists() else None
        maxb = float(self.cfg.get("log_max_mb", 5)) * 1_000_000
        if st and st.st_size > maxb:
            self.logpath.write_bytes(b"")
        lf = open(self.logpath, "ab", buffering=0)
        self.proc = subprocess.Popen(self.d["args"], stdout=subprocess.DEVNULL,
                                     stderr=lf, env=env)
        lf.close()
        self.started_at = time.monotonic()
        self.placed = ""
        log.info("  started %-28s pid=%-6d tile %d,%d %dx%d",
                 self.d["title"], self.proc.pid, self.d["x"], self.d["y"],
                 self.d["w"], self.d["h"])

    def stop(self) -> None:
        """Terminate this pane's process (if any) and clear its state."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.placed = ""

    def tail(self, n: int = 8) -> str:
        try:
            lines = self.logpath.read_text(errors="replace").splitlines()
            return "  ".join(x.strip() for x in lines[-n:]).strip()
        except OSError:
            return "<no log>"


def check_placement(pane: Pane, want: tuple[int, int, int, int],
                    tol: int, may_fix: bool) -> str:
    wid = x11_find(pane.d["title"])
    if wid is None:
        return ""
    g = x11_geom(wid)
    if g is None:
        return ""
    x, y, w, h = g
    ok = (abs(x - want[0]) <= tol and abs(y - want[1]) <= tol
          and abs(w - want[2]) <= tol and abs(h - want[3]) <= tol)
    if ok:
        return "ok"
    note = f"drift got {x},{y} {w}x{h} want {want[0]},{want[1]} {want[2]}x{want[3]}"
    if may_fix and x11_move(wid, want[0], want[1]):
        g2 = x11_geom(wid)
        if g2 and abs(g2[0] - want[0]) <= tol and abs(g2[1] - want[1]) <= tol:
            return "fixed"
    return note


def preview(cfg: dict, panes: list[dict]) -> None:
    for d in panes:
        print(f"\nwindow={d['window_id']} pane={d['pane_id']} "
              f"monitor={d['monitor']} camera={d['camera'] or '(blank)'} "
              f"tile=({d['x']},{d['y']}) {d['w']}x{d['h']}")
        print(f"  url:  {d['url']}")
        print(f"  cmd:  {' '.join(shlex.quote(a) for a in mask_args(d['args']))}")
        if d["camera"]:
            print(f"  log:  {Path(cfg.get('log_dir','logs')) / (re.sub(r'[^A-Za-z0-9._-]+','_', f"{d['window_id']}__{d['pane_id']}")+'.log')}")


_STOP = False


def _sig(signum, _frame):
    global _STOP
    _STOP = True
    log.info("signal %s received, stopping panes...", signum)


def run_wall(cfg: dict, panes: list[dict]) -> int:
    global _STOP
    lp = cfg.get("placement") or {}
    verify = bool(lp.get("verify", True)) and bool(os.environ.get("DISPLAY"))
    may_fix = bool(lp.get("use_xdotool", True)) and bool(shutil.which("xdotool"))
    tol = int(lp.get("max_drift_px", 24))
    stag = float(cfg.get("start_stagger_s", 0.15))
    env = {**os.environ, **(cfg.get("env") or {})}
    panes_out = [Pane(d, cfg) for d in panes]
    mon_panes: dict[str, list[Pane]] = {}
    t0 = time.monotonic()
    for i, p in enumerate(panes_out):
        p.monitor_down = not p.d.get("monitor_connected", True)
        p.next_start = t0 + i * stag
        mon_panes.setdefault(p.d["monitor"], []).append(p)
    mon_conn = {mid: any(not p.monitor_down for p in grp)
                for mid, grp in mon_panes.items() if grp}
    mon_poll = max(1.0, float(cfg.get("monitor_poll_s", 2.0)))

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    log.info("wall: %d pane(s) across %d window(s) | verify=%s xdotool-fix=%s",
             len(panes_out), len({d['window_id'] for d in panes}), verify, may_fix)
    for p in panes_out:
        d = p.d
        if d["camera"] is None:
            log.info("  pane %-28s blank tile  (%d,%d %dx%d)", d["title"], d["x"], d["y"], d["w"], d["h"])
        else:
            log.info("  pane %-28s <- %s [%s] (%d,%d %dx%d)",
                     d["title"], d["url"], d["camera"], d["x"], d["y"], d["w"], d["h"])

    for mid, grp in mon_panes.items():
        if grp and grp[0].monitor_down:
            log.warning("monitor %s absent at startup -- %d pane(s) held until it "
                        "connects: %s", mid, len(grp),
                        ", ".join(p.d["title"] for p in grp))

    checked: dict[str, float] = {}
    next_mon_check = t0 + mon_poll
    try:
        while not _STOP:
            now = time.monotonic()
            if now >= next_mon_check:
                next_mon_check = now + mon_poll
                _poll_monitors(cfg, mon_conn, mon_panes)
            for p in panes_out:
                if p.proc is None:
                    if not p.monitor_down and now >= p.next_start:
                        p.start(env)
                elif p.proc.poll() is not None:
                    code = p.proc.returncode
                    if time.monotonic() - p.started_at > 30:
                        p.fails = 0
                    p.proc = None
                    if not mon_conn.get(p.d["monitor"], True):
                        # the monitor's display is gone; hold until it returns
                        p.monitor_down = True
                        p.fails = 0
                        p.next_start = now
                        log.warning("pane %s died and its monitor %s is absent -- "
                                    "holding until it reconnects",
                                    p.d["title"], p.d["monitor"])
                        continue
                    p.fails += 1
                    backoff = min(15.0, 1.5 ** p.fails) if p.d.get("restart", True) else 1e18
                    p.next_start = now + backoff
                    if p.d.get("restart", True):
                        log.warning("pane %s exited code=%s in %.0fs (%d crash(es)) - retry in %.1fs",
                                    p.d["title"], code, time.monotonic() - p.started_at,
                                    p.fails, backoff)
                    else:
                        log.error("pane %s exited code=%s (restart disabled)", p.d["title"], code)
            # placement checks, throttled per pane
            if verify:
                for p in panes_out:
                    if p.monitor_down or p.proc is None or p.proc.poll() is not None:
                        continue
                    if p.placed and p.placed not in ("ok", "fixed"):
                        continue
                    last = checked.get(p.d["title"], 0.0)
                    if now - last < 5.0 or time.monotonic() - p.started_at < 3.0:
                        continue
                    checked[p.d["title"]] = now
                    st = check_placement(p, (p.d["x"], p.d["y"], p.d["w"], p.d["h"]), tol, may_fix)
                    if st:
                        p.placed = st
                        (log.info if st in ("ok", "fixed") else log.warning)(
                            "placement %s: %s", st, p.d["title"])
            time.sleep(0.5)
    finally:
        log.info("stopping...")
        for p in panes_out:
            p.stop()
        log.info("all panes stopped")
    return 0


def print_monitors() -> int:
    mons = discover_monitors()
    if not mons:
        print("no connected displays found (xrandr?)", file=sys.stderr)
        return 1
    print("Connected displays:")
    for m in mons:
        print(f"  {m['id']:<12} {m['width']}x{m['height']}  at +{m['x']}+{m['y']}")
    print("\nPaste-ready  \"monitors\"  JSON:")
    print(json.dumps({"monitors": mons}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.example.json", help="JSON config file")
    ap.add_argument("--validate", action="store_true", help="check config, print pane grid, exit")
    ap.add_argument("--preview", action="store_true", help="print the exact ffplay command per pane, exit")
    ap.add_argument("--print-monitors", action="store_true", help="list displays from xrandr and exit")
    ap.add_argument("--dry-run", action="store_true", help="alias for --preview")
    ap.add_argument("-v", "--verbose", action="store_true", help="run ffplay with -v (loglevel verbose)")
    ap.add_argument("--log-level", default="INFO", help="launcher log level (DEBUG/INFO/WARNING)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
                        datefmt="%H:%M:%S")

    if args.print_monitors:
        return print_monitors()

    try:
        cfg = load_config(args.config)
        if args.verbose:
            cfg["verbose"] = True
        cfg.setdefault("log_dir", str(Path(Path(args.config).parent) / "logs"))
        panes = validate_config(cfg)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    nwin = len({d['window_id'] for d in panes})
    nwait = sum(1 for d in panes if not d.get("monitor_connected", True))
    print(f"config OK: {len(panes)} pane(s), {nwin} window(s)"
          + (f", {nwait} waiting for an absent monitor" if nwait else ""))
    for d in panes:
        absent = "  [monitor absent]" if not d.get("monitor_connected", True) else ""
        print(f"  [{d['window_id']}/{d['pane_id']}] mon={d['monitor']} "
              f"cam={d['camera'] or '-':<12} ({d['x']},{d['y']}) {d['w']}x{d['h']}{absent}"
              f"  {d['url'][:60]}")

    if args.validate:
        return 0
    if args.preview or args.dry_run:
        preview(cfg, panes)
        return 0

    return run_wall(cfg, panes)


if __name__ == "__main__":
    sys.exit(main())
