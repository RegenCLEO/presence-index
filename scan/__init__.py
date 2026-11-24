"""CLEO Presence Index public scan, protocol PS-1.0.

Each stage is one command that reads files and writes a file:
``python -m scan <stage> --panel panel.yaml --out DIR``.
"""
import sys
from pathlib import Path

# The scoring maths lives in calculator/reference.py and is imported, never copied.
_CALCULATOR = str(Path(__file__).resolve().parents[1] / "calculator")
if _CALCULATOR not in sys.path:
    sys.path.insert(0, _CALCULATOR)

PROTOCOL = "PS-1.0"
