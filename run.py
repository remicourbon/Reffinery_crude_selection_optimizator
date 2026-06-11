#!/usr/bin/env python3
"""One-command launcher for the Streamlit app.

Usage (from anywhere, with the project's Python/venv active):
    python run.py

It guarantees the project root is on sys.path (so `core` imports resolve),
installs any missing dependencies, then launches Streamlit. This removes the
need for PYTHONPATH=... or editing app.py.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    # Make sure the project root is importable in the launched process.
    # Streamlit runs app/app.py in a fresh interpreter, so we pass the root
    # via PYTHONPATH in the environment of the subprocess.
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    # Best-effort: install requirements if streamlit is missing.
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("Installing dependencies (first run)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "-r", str(ROOT / "requirements.txt")], check=True)

    app = ROOT / "app" / "app.py"
    print(f"Launching {app} ...")
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app)],
                   env=env, check=True)


if __name__ == "__main__":
    main()
