import sys
from pathlib import Path

# scripts/ isn't a package; make poll_discord importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
