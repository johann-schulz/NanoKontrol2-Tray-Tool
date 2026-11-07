#!/usr/bin/env bash
#
# Installs nanokontrol-daemon for the current user:
#   - system packages (needs sudo, asks before doing anything)
#   - python packages
#   - the script into ~/.local/bin
#   - a systemd user service bound to the graphical session
#
# Safe to re-run: everything it does is idempotent. Existing configuration in
# ~/.config/nanokontrol is never touched.
#
# Options:
#   --no-service    install everything but do not enable the systemd unit
#   --uninstall     remove the service and the installed script

set -euo pipefail

SCRIPT_NAME="nanokontrol-daemon.py"
SERVICE_NAME="nanokontrol-daemon.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
TARGET="${BIN_DIR}/${SCRIPT_NAME}"

APT_PACKAGES=(
    playerctl            # MPRIS control for the transport buttons
    python3-pip
    libasound2-dev       # build dependency of python-rtmidi
    pkg-config
    python3-gi           # tray icon and settings window
    gir1.2-gtk-3.0
    gir1.2-ayatanaappindicator3-0.1
    libportaudio2        # audio output of the speaker keep-alive
)

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m warning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m error:\033[0m %s\n' "$*" >&2; exit 1; }

install_service=1
mode=install
for arg in "$@"; do
    case "$arg" in
        --no-service) install_service=0 ;;
        --uninstall)  mode=uninstall ;;
        -h|--help)    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            die "unknown option: $arg" ;;
    esac
done

[[ "${EUID}" -eq 0 ]] && die "run this as your normal user, not as root - it installs into your home directory"

# --- Uninstall ---------------------------------------------------------------

if [[ "$mode" == uninstall ]]; then
    info "Removing the systemd user service"
    systemctl --user disable --now "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${SERVICE_NAME}"
    systemctl --user daemon-reload
    info "Removing ${TARGET}"
    rm -f "${TARGET}"
    info "Done. Configuration in ${XDG_CONFIG_HOME:-${HOME}/.config}/nanokontrol was kept."
    exit 0
fi

# --- System packages ---------------------------------------------------------

[[ -f "${SOURCE_DIR}/${SCRIPT_NAME}" ]] || die "${SCRIPT_NAME} not found next to this script"

if command -v apt-get >/dev/null 2>&1; then
    missing=()
    for package in "${APT_PACKAGES[@]}"; do
        dpkg -s "$package" >/dev/null 2>&1 || missing+=("$package")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        info "The following system packages are missing: ${missing[*]}"
        read -rp "Install them with sudo apt install? [Y/n] " reply
        if [[ -z "$reply" || "$reply" =~ ^[Yy] ]]; then
            sudo apt-get update
            sudo apt-get install -y "${missing[@]}"
        else
            warn "Skipping system packages - the daemon may not start"
        fi
    else
        info "All system packages already present"
    fi
else
    warn "No apt-get found. Install these yourself, however your distribution names them:"
    warn "  playerctl, python3-pip, alsa-lib headers, pkg-config,"
    warn "  PyGObject, GTK 3 introspection data, AyatanaAppIndicator3 introspection data"
    warn "  PortAudio (libportaudio2)"
fi

# --- Python packages ---------------------------------------------------------

info "Installing Python packages"
pip_args=(install --user --upgrade -r "${SOURCE_DIR}/requirements.txt")
# Modern distributions mark the system Python as externally managed (PEP 668).
# --user alone is refused there, so retry with the documented escape hatch.
if ! python3 -m pip "${pip_args[@]}" 2>/dev/null; then
    warn "pip refused the plain install (externally managed environment), retrying with --break-system-packages"
    python3 -m pip "${pip_args[@]}" --break-system-packages
fi

# --- The script itself -------------------------------------------------------

info "Installing ${SCRIPT_NAME} to ${BIN_DIR}"
mkdir -p "${BIN_DIR}"
install -m 755 "${SOURCE_DIR}/${SCRIPT_NAME}" "${TARGET}"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) warn "${BIN_DIR} is not in your PATH - the systemd unit uses an absolute path, so this only matters if you want to run the script by name" ;;
esac

# --- systemd user service ----------------------------------------------------

if [[ "$install_service" -eq 1 ]]; then
    info "Writing ${UNIT_DIR}/${SERVICE_NAME}"
    mkdir -p "${UNIT_DIR}"
    cat > "${UNIT_DIR}/${SERVICE_NAME}" <<UNIT
[Unit]
Description=nanoKONTROL2 mixer + transport control
After=graphical-session.target pipewire-pulse.service
PartOf=graphical-session.target
Wants=pipewire-pulse.service

[Service]
ExecStart=$(command -v python3) %h/.local/bin/${SCRIPT_NAME}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
UNIT

    systemctl --user daemon-reload
    info "Enabling and starting the service"
    systemctl --user enable --now "${SERVICE_NAME}"

    sleep 1
    if systemctl --user is-active --quiet "${SERVICE_NAME}"; then
        info "Service is running."
    else
        warn "Service is not running. Most likely the nanoKONTROL2 is not plugged in;"
        warn "systemd will keep retrying. Check with:"
        warn "  journalctl --user -u ${SERVICE_NAME} -f"
    fi
else
    info "Skipping the systemd service as requested (--no-service)"
fi

# --- Post-install hints ------------------------------------------------------

if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; then
    if command -v gnome-extensions >/dev/null 2>&1; then
        if ! gnome-extensions list --enabled 2>/dev/null | grep -qi appindicator; then
            warn "GNOME without an AppIndicator extension: the daemon will run, but there will be no tray icon."
            warn "On Ubuntu the extension ships as gnome-shell-extension-appindicator."
        fi
    fi
fi

info "Done. Click the tray icon to assign faders and knobs."
