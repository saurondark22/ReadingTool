import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
PYCACHE_DIR = ROOT_DIR / "__pycache__"

DAEMON_NAME = "ReadingTool"
WORKER_NAME = "tts_worker"


def remove_path(path: Path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def require_path(path: Path, description: str):
    if not path.exists():
        print(f"ERROR: Missing {description}: {path}")
        sys.exit(1)


def venv_bin(name: str) -> str:
    candidate = ROOT_DIR / ".venv" / "bin" / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    print(f"ERROR: {name} is not available.")
    print("Run: bash build-and-install-local-linux.sh")
    sys.exit(1)


def run_preflight_checks():
    require_path(ROOT_DIR / "main.py", "daemon entrypoint")
    require_path(ROOT_DIR / "tts_worker.py", "worker script")
    require_path(ROOT_DIR / "icons" / "app_icon.png", "application icon")


def run_pyinstaller_build():
    run_preflight_checks()
    pyinstaller = venv_bin("pyinstaller")

    # --- 1. Build the lightweight daemon --------------------------------
    # Excludes the heavy ML/audio libs — those live only in the worker.
    daemon_cmd = [
        pyinstaller,
        "--onefile",
        "--windowed",
        f"--name={DAEMON_NAME}",
        "--clean",
        "--noconfirm",
        "--add-data",
        "config.json:.",
        "--add-data",
        "icons:icons",
        "--collect-all",
        "pynput",
        # Do NOT --collect-all PySide6 — it re-includes all Qt6 libraries
        # (QML, WebEngine, Quick, etc.) even when excluded below. Let
        # PyInstaller's PySide6 hook auto-detect the core modules we import.
        "--hidden-import",
        "darkdetect",
        "--hidden-import",
        "pyperclip",
        # Exclude heavy libs that belong to the worker only
        "--exclude-module",
        "onnxruntime",
        "--exclude-module",
        "kokoro_onnx",
        "--exclude-module",
        "sounddevice",
        "--exclude-module",
        "soundfile",
        "--exclude-module",
        "phonemizer_fork",
        "--exclude-module",
        "espeakng_loader",
        "--exclude-module",
        "csvw",
        "--exclude-module",
        "language_tags",
        # Exclude unused PySide6 modules
        "--exclude-module",
        "tkinter",
        "--exclude-module",
        "unittest",
        "--exclude-module",
        "IPython",
        "--exclude-module",
        "jedi",
        "--exclude-module",
        "PySide6.QtQml",
        "--exclude-module",
        "PySide6.QtQuick",
        "--exclude-module",
        "PySide6.QtQuickWidgets",
        "--exclude-module",
        "PySide6.QtPrintSupport",
        "--exclude-module",
        "PySide6.QtSql",
        "--exclude-module",
        "PySide6.QtTest",
        "--exclude-module",
        "PySide6.QtSvg",
        "--exclude-module",
        "PySide6.QtSvgWidgets",
        "--exclude-module",
        "PySide6.QtHelp",
        "--exclude-module",
        "PySide6.QtMultimedia",
        "--exclude-module",
        "PySide6.QtMultimediaWidgets",
        "--exclude-module",
        "PySide6.QtOpenGL",
        "--exclude-module",
        "PySide6.QtOpenGLWidgets",
        "--exclude-module",
        "PySide6.QtPositioning",
        "--exclude-module",
        "PySide6.QtLocation",
        "--exclude-module",
        "PySide6.QtSerialPort",
        "--exclude-module",
        "PySide6.QtWebChannel",
        "--exclude-module",
        "PySide6.QtWebSockets",
        "--exclude-module",
        "PySide6.QtWinExtras",
        "--exclude-module",
        "PySide6.QtNetworkAuth",
        "--exclude-module",
        "PySide6.QtRemoteObjects",
        "--exclude-module",
        "PySide6.QtTextToSpeech",
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--exclude-module",
        "PySide6.QtWebEngine",
        "--exclude-module",
        "PySide6.QtBluetooth",
        "--exclude-module",
        "PySide6.QtNfc",
        "--exclude-module",
        "PySide6.QtWebView",
        "--exclude-module",
        "PySide6.QtCharts",
        "--exclude-module",
        "PySide6.QtDataVisualization",
        "--exclude-module",
        "PySide6.QtPdf",
        "--exclude-module",
        "PySide6.QtPdfWidgets",
        "--exclude-module",
        "PySide6.Qt3DExtras",
        "--exclude-module",
        "PySide6.QtDesigner",
        "--exclude-module",
        "PySide6.QtGraphs",
        "--exclude-module",
        "PySide6.QtQuick3D",
        "--exclude-module",
        "PySide6.QtQuickControls2",
        "--exclude-module",
        "PySide6.QtQuickParticles",
        "--exclude-module",
        "PySide6.QtQuickTest",
        "--exclude-module",
        "PySide6.QtSensors",
        "--exclude-module",
        "PySide6.QtStateMachine",
        "--exclude-module",
        "PySide6.Qt3DCore",
        "--exclude-module",
        "PySide6.Qt3DRender",
        "--exclude-module",
        "PySide6.Qt3DInput",
        "--exclude-module",
        "PySide6.Qt3DLogic",
        "--exclude-module",
        "PySide6.Qt3DAnimation",
        "--exclude-module",
        "PySide6.Qt3DExtras",
        "main.py",
    ]

    # --- 2. Build the heavy worker (loads Kokoro + plays audio) ----------
    worker_cmd = [
        pyinstaller,
        "--onefile",
        f"--name={WORKER_NAME}",
        "--clean",
        "--noconfirm",
        "--collect-all",
        "kokoro_onnx",
        "--collect-all",
        "phonemizer_fork",
        "--collect-all",
        "espeakng_loader",
        "--collect-all",
        "csvw",
        "--collect-all",
        "language_tags",
        "--collect-all",
        "sounddevice",
        "--collect-all",
        "soundfile",
        "--hidden-import",
        "numpy",
        "--add-data",
        "text_cleaner.py:.",
        # Worker does NOT need PySide6 or pynput
        "--exclude-module",
        "PySide6",
        "--exclude-module",
        "pynput",
        "--exclude-module",
        "darkdetect",
        "--exclude-module",
        "pyperclip",
        "tts_worker.py",
    ]

    try:
        remove_path(DIST_DIR)
        remove_path(BUILD_DIR)
        remove_path(PYCACHE_DIR)

        print("=== Building daemon (light) ===")
        subprocess.run(daemon_cmd, check=True, cwd=ROOT_DIR)
        print(f"Daemon built: {DIST_DIR / DAEMON_NAME}")

        remove_path(BUILD_DIR)
        remove_path(PYCACHE_DIR)

        print("=== Building worker (heavy) ===")
        subprocess.run(worker_cmd, check=True, cwd=ROOT_DIR)
        print(f"Worker built: {DIST_DIR / WORKER_NAME}")

        remove_path(BUILD_DIR)
        remove_path(PYCACHE_DIR)

    except subprocess.CalledProcessError as e:
        print(f"Build failed with error: {e}")
        sys.exit(e.returncode or 1)
    except OSError as e:
        print(f"Build failed with OS error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_pyinstaller_build()
