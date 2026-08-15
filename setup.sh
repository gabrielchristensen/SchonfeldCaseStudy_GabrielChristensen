#!/usr/bin/env bash
set -euo pipefail

required_major=3
required_minor=10
python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (${required_major}, ${required_minor}) else 1)"; then
    echo "Error: Python >=${required_major}.${required_minor} is required (found ${python_version})." >&2
    echo "src/pit.py uses PEP 604 union type syntax ('set | None') that only works on Python ${required_major}.${required_minor}+." >&2
    exit 1
fi

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Setup complete. Activate the environment with: source .venv/bin/activate"
