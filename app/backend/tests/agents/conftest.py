"""Make this directory importable so `from harness import ...` works in every
per-agent test file regardless of where pytest is invoked from."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
