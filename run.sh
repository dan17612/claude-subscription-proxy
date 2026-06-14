#!/usr/bin/env bash
# Entry point for claude-subscription-proxy.
# Creates a local venv on first run, then starts proxy.py.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PYTHON="${PYTHON:-python3}"

if [ ! -d venv ]; then
    "$PYTHON" -m venv venv
    venv/bin/pip install --quiet --upgrade pip
    venv/bin/pip install --quiet -r requirements.txt
fi

exec venv/bin/python proxy.py
