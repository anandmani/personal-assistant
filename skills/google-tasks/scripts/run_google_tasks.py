#!/usr/bin/env python3
"""Run the repository's audited Google Tasks client from the installed skill."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


SKILL_SCRIPT = Path(__file__).resolve()
PRIVATE_DIR = Path.home() / ".config" / "personal-assistant" / "google-tasks"
os.environ.setdefault("PERSONAL_TASKS_APP_DIR", str(PRIVATE_DIR))

for parent in SKILL_SCRIPT.parents:
    client = parent / "google_tasks.py"
    if client.is_file():
        runpy.run_path(str(client), run_name="__main__")
        break
else:
    print("Could not locate the personal-assistant Google Tasks client.", file=sys.stderr)
    raise SystemExit(2)
