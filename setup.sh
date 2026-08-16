#!/usr/bin/env bash
set -euo pipefail

# Windows' official python.org installer only registers `python.exe`,
# not `python3.exe` -- WSL does ship python3, but a Git Bash + native
# Windows Python setup may not. Prefer python3 (the standard on macOS/
# Linux/WSL), fall back to python so this still works there too.
PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python

required_major=3
required_minor=10
python_version=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if ! "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (${required_major}, ${required_minor}) else 1)"; then
    echo "Error: Python >=${required_major}.${required_minor} is required (found ${python_version})." >&2
    echo "src/pit.py uses PEP 604 union type syntax ('set | None') that only works on Python ${required_major}.${required_minor}+." >&2
    exit 1
fi

"$PYTHON" -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Setup complete. Activate the environment with: source .venv/bin/activate"
