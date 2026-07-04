#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SOURCE="${SCRIPT_DIR}"
ENABLE_AUTOSTART=""

print_usage() {
  cat <<'USAGE'
Usage:
  bash install-local-linux.sh [--app-source <dir>] [--enable-autostart|--disable-autostart]

Installs ReadingTool for the current Linux user:
- App files: ~/.local/share/readingtool/app
- Launcher command: ~/.local/bin/reading-tool
- App menu entry: ~/.local/share/applications/reading-tool.desktop

Options:
  --app-source <dir>   Source directory containing app assets and dist output
  --enable-autostart   Create ~/.config/autostart/reading-tool.desktop
  --disable-autostart  Remove ~/.config/autostart/reading-tool.desktop
  -h, --help           Show this help message
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-source)
      shift
      if [[ $# -eq 0 ]]; then
        echo "ERROR: --app-source requires a value"
        exit 1
      fi
      APP_SOURCE="$1"
      ;;
    --enable-autostart)
      ENABLE_AUTOSTART="yes"
      ;;
    --disable-autostart)
      ENABLE_AUTOSTART="no"
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1"
      print_usage
      exit 1
      ;;
  esac
  shift
done

if [[ "${EUID}" -eq 0 ]]; then
  echo "ERROR: Please run this as your regular desktop user, not root."
  exit 1
fi

if [[ ! -d "${APP_SOURCE}" ]]; then
  echo "ERROR: App source directory not found: ${APP_SOURCE}"
  exit 1
fi

DIST_DAEMON=""
DIST_WORKER=""
if [[ -x "${APP_SOURCE}/ReadingTool" ]]; then
  DIST_DAEMON="${APP_SOURCE}/ReadingTool"
elif [[ -x "${APP_SOURCE}/dist/ReadingTool" ]]; then
  DIST_DAEMON="${APP_SOURCE}/dist/ReadingTool"
else
  echo "ERROR: Daemon binary not found in ${APP_SOURCE}"
  echo "Build first with: python3 pyinstaller-build-script.py"
  exit 1
fi

if [[ -x "${APP_SOURCE}/tts_worker" ]]; then
  DIST_WORKER="${APP_SOURCE}/tts_worker"
elif [[ -x "${APP_SOURCE}/dist/tts_worker" ]]; then
  DIST_WORKER="${APP_SOURCE}/dist/tts_worker"
else
  echo "ERROR: TTS Worker binary not found in ${APP_SOURCE}"
  echo "Build first with: python3 pyinstaller-build-script.py"
  exit 1
fi

if [[ ! -d "${APP_SOURCE}/icons" ]]; then
  echo "ERROR: Missing required directory: ${APP_SOURCE}/icons"
  exit 1
fi

if [[ ! -f "${APP_SOURCE}/config.json" ]]; then
  echo "ERROR: Missing required file: ${APP_SOURCE}/config.json"
  exit 1
fi

DATA_HOME="${XDG_DATA_HOME:-${HOME}/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
INSTALL_ROOT="${DATA_HOME}/readingtool"
APP_DIR="${INSTALL_ROOT}/app"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${DATA_HOME}/applications"
AUTOSTART_DIR="${CONFIG_HOME}/autostart"
LAUNCHER_PATH="${BIN_DIR}/reading-tool"
DESKTOP_PATH="${APPS_DIR}/reading-tool.desktop"
AUTOSTART_PATH="${AUTOSTART_DIR}/reading-tool.desktop"

mkdir -p "${APP_DIR}" "${BIN_DIR}" "${APPS_DIR}" "${AUTOSTART_DIR}"

install -m 0755 "${DIST_DAEMON}" "${APP_DIR}/ReadingTool"
install -m 0755 "${DIST_WORKER}" "${APP_DIR}/tts_worker"

rm -rf "${APP_DIR}/icons"
cp -a "${APP_SOURCE}/icons" "${APP_DIR}/icons"

# Preserve user-customized config on upgrades.
if [[ ! -f "${APP_DIR}/config.json" ]]; then
  install -m 0644 "${APP_SOURCE}/config.json" "${APP_DIR}/config.json"
fi

cat > "${LAUNCHER_PATH}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/readingtool/app"
if [[ -d "${APP_DIR}/lib" ]]; then
  export LD_LIBRARY_PATH="${APP_DIR}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi
if [[ -d "${APP_DIR}/bin" ]]; then
  export PATH="${APP_DIR}/bin:${PATH}"
fi
cd "${APP_DIR}"
exec "${APP_DIR}/ReadingTool" "$@"
EOF
chmod 0755 "${LAUNCHER_PATH}"

cat > "${DESKTOP_PATH}" <<EOF
[Desktop Entry]
Type=Application
Name=ReadingTool
Comment=Read selected text aloud via Kokoro TTS, triggered by a global hotkey
Exec=${LAUNCHER_PATH}
Icon=${APP_DIR}/icons/app_icon.png
Terminal=false
Categories=Office;Utility;AudioVideo;
StartupNotify=false
EOF
chmod 0644 "${DESKTOP_PATH}"

if [[ "${ENABLE_AUTOSTART}" == "yes" ]]; then
  cat > "${AUTOSTART_PATH}" <<EOF
[Desktop Entry]
Type=Application
Name=ReadingTool
Comment=Start ReadingTool in background at login
Exec=${LAUNCHER_PATH}
Icon=${APP_DIR}/icons/app_icon.png
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
  chmod 0644 "${AUTOSTART_PATH}"
  echo "Autostart enabled at: ${AUTOSTART_PATH}"
elif [[ "${ENABLE_AUTOSTART}" == "no" ]]; then
  rm -f "${AUTOSTART_PATH}"
  echo "Autostart disabled."
fi

echo "Install complete for user: ${USER}"
echo "Launcher command: reading-tool"
echo "Desktop entry: ${DESKTOP_PATH}"

if ! command -v xclip >/dev/null 2>&1 && ! command -v xsel >/dev/null 2>&1 && ! command -v wl-copy >/dev/null 2>&1; then
  echo "WARNING: No clipboard backend detected (xclip/xsel/wl-copy)."
  echo "         Install one to ensure selection capture works."
fi

if [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
  echo "NOTICE: Running in Wayland session; global hotkey/focus behavior may be limited."
fi

if ! ldconfig -p 2>/dev/null | grep -q "libportaudio"; then
  echo "WARNING: PortAudio library not found (libportaudio2)."
  echo "         Audio playback will not work until installed:"
  echo "         sudo apt install libportaudio2"
fi