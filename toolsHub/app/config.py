"""Central configuration for toolsHub.

All paths are resolved relative to this file's location so the app works
correctly regardless of the working directory it is launched from.

Directory layout assumed:
  open-jobs/
  ├── lib/          ← shared ML helpers
  ├── work/         ← data directory (created here if absent)
  └── toolsHub/
      └── app/
          └── config.py  ← this file
"""
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Project root: open-jobs/
_ROOT = Path(__file__).resolve().parents[2]

# Shared ML helpers (locparse, seniority, salary)
LIB = _ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

# Data directory — override with the WORK env var (same contract as tools/jobs.py)
WORK = Path(os.environ.get("WORK", str(_ROOT / "work")))
WORK.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Backend URLs
# ---------------------------------------------------------------------------

BASE = os.environ.get("WORKER_URL", "https://backend.dehnbostele.workers.dev")
# DATA_URL lets you point /data/* at a local mirror without changing the main worker URL
DATA = os.environ.get("DATA_URL", BASE)

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

UA = {"user-agent": "open-jobs-toolshub/0.1"}
