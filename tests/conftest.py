import os
import sys
from pathlib import Path

# Make src importable without installation.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Ensure unit tests never accidentally read a real secrets file.
os.environ.setdefault("TEA_PG_PASSWORD", "test")
os.environ.setdefault("TEA_TRADING_ENV", "dev")
