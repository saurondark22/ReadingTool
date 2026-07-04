#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_FLAG=""
SKIP_BUILD="no"

print_usage() {
  cat <<'USAGE'
Usage:
  bash build-and-install-local-linux.sh [--enable-autostart|--disable-autostart] [--skip-build]

Builds ReadingTool with PyInstaller, then installs it locally with:
- Launcher command: ~/.local/bin/reading-tool
- App menu entry: ~/.local/share/applications/reading-tool.desktop

Options:
  --enable-autostart   Enable autostart after install
  --disable-autostart  Disable autostart after install
  --skip-build         Skip PyInstaller build and only run local install
  -h, --help           Show this help message
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable-autostart|--disable-autostart)
      AUTOSTART_FLAG="$1"
      ;;
    --skip-build)
      SKIP_BUILD="yes"
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

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required but was not found."
  exit 1
fi

INSTALL_SCRIPT="${SCRIPT_DIR}/install-local-linux.sh"
if [[ ! -f "${INSTALL_SCRIPT}" ]]; then
  echo "ERROR: Missing installer script at ${INSTALL_SCRIPT}"
  exit 1
fi

pushd "${SCRIPT_DIR}" >/dev/null

# --- Python environment --------------------------------------------------
# Modern Debian/Ubuntu blocks system-wide pip (PEP 668). Create a venv and
# install requirements there so the build is self-contained and one-command.
VENV_DIR="${SCRIPT_DIR}/.venv"
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "Creating Python virtual environment at ${VENV_DIR}..."
  python3 -m venv "${VENV_DIR}"
fi

echo "Installing build dependencies into venv..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r requirements.txt

if [[ "${SKIP_BUILD}" != "yes" ]]; then
  echo "Building with PyInstaller..."
  "${VENV_DIR}/bin/python3" pyinstaller-build-script.py
else
  echo "Skipping build as requested."
fi

echo "Installing locally for current user..."
install_args=(--app-source "${SCRIPT_DIR}")
if [[ -n "${AUTOSTART_FLAG}" ]]; then
  install_args+=("${AUTOSTART_FLAG}")
fi
bash "${INSTALL_SCRIPT}" "${install_args[@]}"

popd >/dev/null

echo "Done. Launch via: reading-tool"