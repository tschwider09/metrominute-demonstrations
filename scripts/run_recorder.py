#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if __name__ == "__main__":
    try:
        from metrominute_rt.realtime_recorder import main
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "dependency")
        raise SystemExit(
            f"Missing dependency: {missing}. "
            "Install project requirements first with: pip install -r requirements.txt"
        ) from exc
    raise SystemExit(main())
