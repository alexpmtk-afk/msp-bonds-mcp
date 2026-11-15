"""Project-local runtime paths; persist across source tree reorganizations."""
from pathlib import Path


def runtime_dir() -> Path:
    # .../project/src/msp_bonds_mcp/core/paths.py -> project/runtime
    return Path(__file__).resolve().parents[3] / "runtime"
