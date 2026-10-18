#!/usr/bin/env python3
"""Entry point for the WorkBuddy local bridge.

Kept outside the ``wbclient`` package so it can be launched by absolute path
(the background server and the SessionStart hook both do this).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wbclient.bridge import main  # noqa: E402

if __name__ == "__main__":
    # Under pythonw.exe (used so the bridge never gets a console window) the
    # standard streams can be absent; ``print`` would raise on None.
    for stream_name in ("stdout", "stderr"):
        if getattr(sys, stream_name) is None:
            setattr(sys, stream_name, open(os.devnull, "w", encoding="utf-8"))
    sys.exit(main())
