#!/usr/bin/env bash
#
# install_service.sh -- install watch_cameras.py as a system service that starts
# once the LXDE desktop (X11 session) is ready.
#
#   ./install_service.sh            # install + enable only (service stays stopped)
#   ./install_service.sh start      # install + start now (waits for the X display)
#   ./install_service.sh uninstall  # stop, disable and remove the service
#
# What it installs (both files are generated -- rerun this script to change):
#
#   /etc/systemd/system/watch-cameras-wait.sh
#       Polls until the X11 display used by the LXDE session answers xrandr,
#       then execs watch_cameras.py as the session user, using that display's
#       XAUTHORITY. If the display is still not up after WAIT_TIMEOUT_S it
#       exits non-zero, so systemd's Restart=on-failure retries (about every
#       10 s) and the wall comes up as soon as LXDE is ready.
#
#   /etc/systemd/system/watch-cameras.service
#       The systemd unit: runs as the LXDE session user, respawns on failure,
#       enabled for boot (multi-user.target).
#
# Tunables (env vars at install time):
#   RUN_USER=pi ./install_service.sh           LXDE session user (auto-detected)
#   WAIT_TIMEOUT_S=300 ./install_service.sh    display wait budget (default 180)
#   WAIT_POLL_S=2 ./install_service.sh         poll interval (default 2)
#   PYTHON=/usr/bin/python3 ./install_service.sh
#
# Logs after installing:
#   journalctl -u watch-cameras                wait wrapper output + unit errors
#   <project>/logs/service.out.log             supervisor + ffplay stdout/stderr
#   <project>/logs/<window>__<pane>.log        per-pane ffplay logs
#
set -euo pipefail

# -----------------------------------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$PROJECT_DIR/watch_cameras.py"
CONFIG="$PROJECT_DIR/config.json"

RUN_USER="${RUN_USER:-}"
WAIT_TIMEOUT_S="${WAIT_TIMEOUT_S:-180}"
WAIT_POLL_S="${WAIT_POLL_S:-2}"
PYTHON="${PYTHON:-python3}"

WAIT_SH="/etc/systemd/system/watch-cameras-wait.sh"
UNIT="/etc/systemd/system/watch-cameras.service"

ACTION="${1:-install}"
case "$ACTION" in
    install|start|uninstall) ;;
    *) printf 'usage: %s [install|start|uninstall]\n' "$0" >&2; exit 2 ;;
esac

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }
esc()  { printf '%q' "$*"; }   # shell-escape a value for the generated files

if [ ! -d /etc/systemd/system ]; then
    die "no systemd found -- this installs a systemd system service"
fi
if [ "$(id -u)" != 0 ]; then
    die "not running as root -- rerun with:  sudo $0 ${1:-install}"
fi
if [ ! -f "$SCRIPT" ]; then
    die "script not found:  $SCRIPT"
fi
if [ ! -f "$CONFIG" ]; then
    die "config not found:  $CONFIG  (cp config.example.json config.json first)"
fi
if ! command -v ffplay >/dev/null 2>&1; then
    warn "ffplay not found on PATH -- the service will fail until it is installed"
fi
if ! command -v xrandr >/dev/null 2>&1; then
    warn "xrandr not found on PATH -- the service needs it (x11-utils)"
fi
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" \
    || die "$PYTHON is not usable"
log "using $PYTHON ($PYVER)"

# ---- discover the LXDE session user -------------------------------------------
if [ -z "$RUN_USER" ]; then
    # lightdm's per-user directory is named after the login user
    for d in /var/run/lightdm/user-*/; do
        if [ -e "${d}env" ]; then
            RUN_USER="$(basename "$d" | sed 's/^user-//')"
            break
        fi
    done
fi
if [ -z "$RUN_USER" ]; then
    # fall back to the first normal user with a /home dir
    RUN_USER="$(awk -F: '$3 >= 1000 && $6 ~ /^\/home\// { print $1; exit }' /etc/passwd)"
fi
if [ -z "$RUN_USER" ]; then
    die "could not auto-detect the LXDE session user -- rerun with RUN_USER=youruser $0"
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
    die "user '$RUN_USER' does not exist"
fi
RUN_USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
XAUTH="$RUN_USER_HOME/.Xauthority"

# -----------------------------------------------------------------------------
if [ "$ACTION" = uninstall ]; then
    log "removing service..."
    systemctl stop watch-cameras  >/dev/null 2>&1 || true
    systemctl disable watch-cameras >/dev/null 2>&1 || true
    rm -f "$WAIT_SH" "$UNIT"
    systemctl daemon-reload >/dev/null 2>&1 || true
    log "uninstalled ($CONFIG and logs/ are untouched)"
    exit 0
fi

# keep the per-pane log dir present + writable by the service user
mkdir -p "$PROJECT_DIR/logs"
chmod 775 "$PROJECT_DIR/logs" 2>/dev/null || true
chown "$RUN_USER" "$PROJECT_DIR/logs" 2>/dev/null || true

# ---- write the X-ready wait wrapper --------------------------------------------
# NOTE: deliberately UNQUOTED heredoc -- values are expanded at install time;
# every \$ below must survive to runtime.
log "writing wait wrapper -> $WAIT_SH"
cat > "$WAIT_SH" <<EOF
#!/usr/bin/env bash
# Generated by install_service.sh -- do not edit; rerun the installer instead.
# Wait until the X11 display used by the LXDE session is ready, then exec the
# camera wall. Exiting non-zero lets systemd's Restart= try again later.
set -u
USER="$RUN_USER"
XAUTH=$(esc "$XAUTH")
TIMEOUT=$(esc "$WAIT_TIMEOUT_S")
POLL=$(esc "$WAIT_POLL_S")
: "\${HOME:=$(esc "$RUN_USER_HOME")}"

display=""
auth=""

# lightdm per-user env file for this user (empty if the session is not up)
ldf() {
    ls -d /var/run/lightdm/user-\$USER-*/env 2>/dev/null | head -n1
}
# X sockets currently present
x_sockets() {
    ls /tmp/.X11-unix/X* /root/.X11-unix/X* 2>/dev/null | sed -E 's|.*/X||' | sort -n
}

# A display is "ready" when xrandr answers on it (X server up + auth accepted).
# XAUTHORITY is passed explicitly, so a stale value is never inherited.
try_display() {   # try_display <display> [xauth-file]
    local d="\$1" a="\${2:-}"
    command -v xrandr >/dev/null 2>&1 || return 1
    DISPLAY="\$d" XAUTHORITY="\$a" xrandr --listmonitors >/dev/null 2>&1
}

end=\$(( \$(date +%s) + TIMEOUT ))
while :; do
    # 1) lightdm per-user session (preferred: its exact DISPLAY + XAUTHORITY)
    l="\$(ldf)"
    if [ -n "\$l" ]; then
        ld_display="\$(sed -n 's/^DISPLAY=//p' "\$l" 2>/dev/null | head -n1)"
        ld_xauth="\$(sed -n 's/^XAUTHORITY=//p' "\$l" 2>/dev/null | head -n1)"
        if [ -n "\$ld_display" ] && [ -n "\$ld_xauth" ] \
           && try_display "\$ld_display" "\$ld_xauth"; then
            display="\$ld_display"; auth="\$ld_xauth"; break
        fi
    fi
    # 2) any live X socket, authenticating with the session user's cookie jar
    for n in \$(x_sockets); do
        if try_display ":\$n" "\$XAUTH"; then
            display=":\$n"; auth="\$XAUTH"; break 2
        fi
    done
    # 3) last resort: :0 with lightdm's root-side cookie (single-user kiosk box)
    c="\$(grep -oE '[0-9a-fA-F]{18,}' /var/run/lightdm/root/:0 2>/dev/null | head -n1)"
    if [ -n "\$c" ]; then
        printf 'host   0   MIT-MAGIC-COOKIE-1 %s\n' "\$c" > "\$HOME/.watch-cameras.cookie"
        chmod 600 "\$HOME/.watch-cameras.cookie" 2>/dev/null
        if try_display ":0" "\$HOME/.watch-cameras.cookie"; then
            display=":0"; auth="\$HOME/.watch-cameras.cookie"; break 2
        fi
    fi
    [ \$(date +%s) -ge \$end ] && break
    sleep "\$POLL"
done

if [ -z "\$display" ]; then
    echo "[watch-cameras] no ready X display for user \$USER within \$TIMEOUT s; exiting so systemd can retry" >&2
    exit 1
fi
if [ -n "\${auth:-}" ] && [ ! -f "\$auth" ] && [ "\$auth" != "\$HOME/.watch-cameras.cookie" ]; then
    auth=""
fi

export USER="\$USER" HOME="\$HOME" DISPLAY="\$display" PATH="/usr/local/bin:/usr/bin:/bin"
if [ -n "\${auth:-}" ]; then
    export XAUTHORITY="\$auth"
fi
unset XDG_RUNTIME_DIR   # stale values from previous logins can confuse X clients

echo "[watch-cameras] display \$DISPLAY is ready -- starting camera wall"
exec $(esc "$PYTHON") $(esc "$SCRIPT") -c $(esc "$CONFIG") \
    >> $(esc "$PROJECT_DIR/logs/service.out.log") 2>&1
EOF
chmod 755 "$WAIT_SH"

# ---- write the systemd unit ------------------------------------------------------
log "writing unit file  -> $UNIT"
cat > "$UNIT" <<EOF
# Generated by install_service.sh -- do not edit; rerun the installer instead.
[Unit]
Description=ffplay camera wall (watch_cameras.py) for the $RUN_USER LXDE session
# LXDE is started by the display manager; wait for it plus a usable network so
# the RTSP streams have a chance to connect.
After=network-online.target lightdm.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$(esc "$PROJECT_DIR")
ExecStart=$WAIT_SH
Environment=HOME=$(esc "$RUN_USER_HOME")
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=10
# No start-rate limiting: the wrapper exits quickly when no display is present
# (e.g. before the display is up), so each retry simply re-checks about every
# 10 s and the wall comes up as soon as LXDE is ready.
StartLimitIntervalSec=0
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"

systemctl daemon-reload
log "LXDE session user : $RUN_USER (XAUTHORITY: $XAUTH)"
log "display wait      : poll every ${WAIT_POLL_S}s, give up after ${WAIT_TIMEOUT_S}s, systemd retries after 10s"
log "installed         : $WAIT_SH"
log "                    $UNIT"
log "service is enabled; to start it:"
log "    systemctl start watch-cameras"
log "    journalctl -u watch-cameras -f"
log "    tail -f $PROJECT_DIR/logs/service.out.log"

case "$ACTION" in
    install) ;;
    start)
        systemctl enable --now watch-cameras
        sleep 2
        systemctl status watch-cameras --no-pager || true
        ;;
esac
