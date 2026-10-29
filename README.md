# ffplay-quad-split

Vibe Coded, JSON-driven, low-latency multi-pane **ffplay** camera wall for X11.

Standard `ffplay` accepts exactly one input per process (a second `-i` is
rejected), so multi-view walls are built from several borderless ffplay
windows positioned with `-left/-top`. A supervisor (`watch_cameras.py`)
launches one ffplay per pane, verifies placement via `xprop`/`xwininfo`,
and restarts any tile that dies — without touching the others.

```
fullscreen window = 1 ffplay, scaled to the whole monitor
2x2 split         = 4 borderless ffplay tiles
```

One process per pane means one dead camera only kills its own tile, and a
reconnect problem in one stream never blocks the rest of the wall.

## Features

- **Tile isolation & auto-restart** — each pane is its own ffplay process;
  crashes are retried with exponential backoff (capped at 15 s), resets after
  30 s of stable running.
- **Low-latency RTSP defaults** — `tcp` transport, `nobuffer`, `low_delay`,
  `discardcorrupt`, small `probesize`/`analyzeduration`, slice threading,
  optional frame drop + `-infbuf`, and optional in-stream reconnect.
  Per-camera overrides on top of the global `latency` block.
- **Auto monitor detection** — monitor entries need only an `"id"`; geometry
  is read from `xrandr` at startup. Explicit geometry in the config
  overrides the live reading (handy for `--validate` on headless boxes).
- **Placement verification** — after boot, each window's geometry is checked
  against the intended tile with `xprop`/`xwininfo`. If `xdotool` is present
  and enabled, a window that a WM shifted gets moved back.
- **Pane labels** — optional `drawtext` overlay (camera label in top-left)
  rendered via a per-pane label file.
- **Blank tiles** — a `split` window with fewer panes than `rows*cols` fills
  the rest with black (`color=black` lavfi sources).
- **Per-pane logging** — each ffpane writes to `./logs/<window>__<pane>.log`,
  auto-truncated when it exceeds `log_max_mb`.
- **Credentials handled safely** — `username`/`password` are percent-encoded
  into the RTSP URL (special chars like `?` or `:` in passwords work), and
  are masked as `***` in logs and `--preview` output.

## Requirements

- Linux with X11 (`DISPLAY` set)
- `ffplay` (FFmpeg) on `PATH`
- `xrandr`, `xprop`, `xwininfo` (x11-utils) — required for monitor geometry
  and placement verification
- `xdotool` — optional, only for auto-fixing windows a WM shifted
- A font for labels, if `labels` is enabled: DejaVu or Liberation Sans
  (`/usr/share/fonts/truetype/dejavu/...` or `.../liberation/...`)

## Quick start

```sh
# 1. See which displays are connected
python3 watch_cameras.py --print-monitors

# 2. Copy and edit the config
cp config.example.json config.json
$EDITOR config.json

# 3. Check the config (works headless if monitors have explicit geometry)
python3 watch_cameras.py -c config.json --validate

# 4. See the exact ffplay command per pane (credentials masked)
python3 watch_cameras.py -c config.json --preview

# 5. Run the wall (Ctrl-C stops everything)
python3 watch_cameras.py -c config.json

# Verbose ffplay logs while debugging a stream
python3 watch_cameras.py -c config.json -v
```

## Command line

| flag | meaning |
|---|---|
| `-c, --config FILE` | JSON config (default `config.example.json`) |
| `--validate` | check config, print the pane grid, exit |
| `--preview` / `--dry-run` | print the exact ffplay argv per pane, exit |
| `--print-monitors` | list displays from `xrandr` with paste-ready JSON, exit |
| `-v, --verbose` | run ffplay with `-loglevel verbose` |
| `--log-level DEBUG\|INFO\|WARNING` | log level of the supervisor itself |

## Config reference

All sections are optional; sensible defaults are applied.

### `cameras`

| key | type | notes |
|---|---|---|
| `id` | string | unique, referenced from windows' panes |
| `url` | string | stream URL (RTSP, RTSPS, or a plain file path) |
| `username` / `password` | string | only added if the URL has no credentials; percent-encoded |
| `label` | string | on-screen text (defaults to `id`); used with `labels` |
| `latency` | object | per-camera overrides of the global `latency` block (use `rtsp_transport` here) |
| `input_extra` | list[str] | extra raw ffplay input options (inserted before `-i`) |

Note: `config.example.json` also carries a legacy per-camera `transport` key; it is ignored — set `latency.rtsp_transport` instead.
| `restart` | (on pane, not camera) | see below |

### `monitors`

```json
{ "id": "rdp-0" }                                   // geometry auto-detected
{ "id": "HDMI-0", "x": 0, "y": 0, "width": 1920, "height": 1080 }  // explicit
```

`id` must match an `xrandr` output name. Run `--print-monitors` to list the
live values. Explicit `x`/`y`/`width`/`height` always win over the live
reading.

### `windows`

| key | type | default | notes |
|---|---|---|---|
| `id` | string | — | window name, used in log file names & window titles |
| `monitor` | string | — | monitor `id` this window belongs to |
| `layout` | `"full"` \| `"split"` | `"full"` | `full` = whole monitor, exactly 1 pane; `split` = grid |
| `rows` / `cols` | int | 2 / 2 | grid size, only for `split` |
| `gap` | int px | 8 | border around and between tiles |
| `panes` | list | — | one entry per grid cell, row-major. `{"camera": "cam1"}` or `{"blank": true}`; optional `id`, optional `restart` (default `true`; set `false` to leave a dead pane dark) |
| `global_extra` | list[str] | — | extra raw ffplay options for every pane in this window |

A `split` window with fewer panes than `rows*cols` pads the remaining cells
with blank black tiles.

### `latency`

Global defaults, overridable per camera.

| key | default | notes |
|---|---|---|
| `rtsp_transport` | `"tcp"` | `tcp` (low reordering, higher delay) or `udp` |
| `connect_timeout_ms` | 1000 | TCP connect timeout |
| `io_timeout_ms` | — | SO timeouts over the stream (`stimeout` URL param) |
| `keepalive` | `false` | adds `rtsp_flags=keepalive` to the URL |
| `wallclock_ts` | `true` | `-use_wallclock_as_timestamps 1` |
| `probesize` | 32768 | bytes probed before first frame |
| `analyzeduration` | 0 | — |
| `thread_type` | `"slice"` | decodes one frame in parallel; lowest per-frame CPU time |
| `framedrop` | `true` | adds `-framedrop -infbuf -max_delay 0` |
| `reconnect_delay_max_s` | 0 | >0 enables ffmpeg's in-stream RTSP reconnect for brief glitches |
| `hardware_decoders` | `[]` | list for repeated `-hwaccel` (e.g. `["vaapi", "auto"]`); `"auto"` alone lets ffplay auto-detect |

### `placement`

| key | default | notes |
|---|---|---|
| `verify` | `true` | check each window's geometry after boot |
| `use_xdotool` | `true` | move a drifted window back (requires `xdotool`) |
| `max_drift_px` | 24 | tolerance for position/size checks |
| `alwaysontop` | `false` | pass `-alwaysontop` to ffplay |
| `override_redirect` | `false` | pass `-n` (window bypasses the WM entirely — no title bar, but the WM can't move it either) |

### Top-level

| key | default | notes |
|---|---|---|
| `ffplay` | `"ffplay"` | path/binary for the ffplay executable |
| `labels` | `true` | drawtext camera label on each pane |
| `start_stagger_s` | 0.15 | delay between launching successive panes (avoids a connect storm) |
| `log_dir` | `<config dir>/logs` | per-pane ffplay logs + label files |
| `log_max_mb` | 5 | truncate a pane's log when it grows past this |
| `verbose` | `false` | same as the `-v` flag, but in-file |
| `env` | `{}` | extra environment variables for ffplay processes |

## Files

```
watch_cameras.py       the whole tool (config parser + supervisor)
install_service.sh     installs the tool as a systemd service started when
                       the LXDE/X11 session is ready
config.example.json    annotated example config (safe to commit)
config.smoke.json      small smoke-test config
config.json            your real config (git-ignored, has credentials)
logs/                  per-pane ffplay logs + label .txt files (git-ignored)
```

## Running as a service (starts with the LXDE session)

`install_service.sh` installs the wall as a systemd system service that
starts on boot **once the LXDE desktop / X11 display is actually ready** (it
polls for a display that answers `xrandr`, then execs the supervisor as your
login user using that session's `XAUTHORITY`. If the display isn't up yet
within the wait budget it exits and systemd retries every ~10 s, so the wall
appears as soon as LXDE is on screen).

```sh
sudo ./install_service.sh           # install + enable (systemd units only)
sudo ./install_service.sh start     # install + start now (waits for the display)
sudo ./install_service.sh uninstall # stop, disable, remove
```

Tunables at install time: `RUN_USER=pi`, `WAIT_TIMEOUT_S=300`,
`WAIT_POLL_S=2`, `PYTHON=/usr/bin/python3`.

It writes (and regenerates on every install):

- `/etc/systemd/system/watch-cameras-wait.sh` — display-readiness wait loop
- `/etc/systemd/system/watch-cameras.service` — unit (runs as the session
  user, `Restart=on-failure`, `WantedBy=multi-user.target`)

Useful afterwards:

```sh
systemctl status|start|stop|disable watch-cameras
journalctl -u watch-cameras -f          # wait wrapper + unit output
tail -f logs/service.out.log            # supervisor + ffplay stdout/stderr
```

Notes: the config path is baked into the generated files at install time, so
re-run `sudo ./install_service.sh` after moving the project or switching
configs. The service enables itself for boot (`WantedBy=multi-user.target`);
no autologin/panel changes are needed.

## Troubleshooting

- **"monitor 'X': missing x, y, width, height"** — the id isn't found by
  `xrandr`. Run `--print-monitors` and copy the exact id.
- **Window drifts / shifted by the WM** — check `placement.use_xdotool` and
  that `xdotool` is installed; supervisor logs a `placement fixed:` line when
  it re-moves a window. Enable `override_redirect` only if your WM keeps
  stealing positions and you don't need WM management.
- **Pane keeps crashing** — open `logs/<window>__<pane>.log` for the actual
  ffplay error (codec, auth, timeout). Common culprits: wrong RTSP path,
  camera-side session limits, or a stream that needs `udp` instead of `tcp`.
- **`Padded dimensions cannot be smaller than input`** — shouldn't happen
  (tile sizes are floored to even numbers); if you see it, make sure your
  monitor geometry in the config matches the live display.
- **No labels drawn** — install `fonts-dejavu-core` (or `fonts-liberation`),
  or set `"labels": false`.

## Notes

- Tile sizes are floored to even numbers because
  `scale=...force_original_aspect_ratio=decrease` rounds the fitted frame to
  codec-friendly dimensions, and `pad` refuses to pad from a frame larger
  than its target.
- Window titles are `<window>::<pane>` (truncated to 63 chars) and are how
  the supervisor finds windows in the X client list — keep pane ids
  reasonably short.
- Ctrl-C / SIGTERM cleanly terminates all panes.
