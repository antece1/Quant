#!/usr/bin/env python3
"""
Shared path resolution for the Options tree.

Why this exists: the tools in tools/ are ticker-agnostic, but the chain
snapshots they read live next to the analysis that produced them (NBIS/,
COHR/, ...). Bare relative filenames only worked while everything sat in one
flat directory. These helpers locate data anywhere under Options/ so a tool
runs correctly from any working directory.

    from optlib import find, latest_chain, data_dir
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"

# Importing optlib puts the tree on sys.path, so any script in a subfolder can
# `from moomoo_option_price import ...` without knowing where tools/ lives.
import sys as _sys
for _p in (ROOT, TOOLS):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))


def bootstrap() -> None:
    """Put Options/ on sys.path. Call from a script in a subfolder."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def find(name: str) -> str:
    """Locate a data file anywhere under Options/ (cwd wins if it exists)."""
    p = Path(name)
    if p.is_file():
        return str(p)
    hits = sorted(ROOT.rglob(name))
    if not hits:
        raise FileNotFoundError(f"{name!r} not found anywhere under {ROOT}")
    return str(hits[0])


def latest_chain(ticker: str) -> str:
    """Newest <ticker>_chain_YYYY-MM-DD.json in the tree. Filenames sort by date."""
    hits = sorted(ROOT.rglob(f"{ticker.lower()}_chain_*.json"))
    if not hits:
        raise FileNotFoundError(
            f"no chain snapshot for {ticker.upper()} under {ROOT}. "
            f"Run: ./.venv/bin/python Options/tools/leaps_chain_pull.py {ticker.upper()}")
    return str(hits[-1])


def chain_for(ticker: str, asof: str | None = None) -> str:
    """Chain for a specific date, else the newest one."""
    if asof:
        return find(f"{ticker.lower()}_chain_{asof}.json")
    return latest_chain(ticker)


def data_dir(ticker: str) -> Path:
    """Where a new snapshot for this ticker belongs; created on demand."""
    d = ROOT / ticker.upper()
    d.mkdir(exist_ok=True)
    return d


if __name__ == "__main__":
    print(f"Options root: {ROOT}")
    for t in sorted({p.name.split("_chain_")[0] for p in ROOT.rglob("*_chain_*.json")}):
        snaps = sorted(p.name for p in ROOT.rglob(f"{t}_chain_*.json"))
        print(f"  {t.upper():<6} {len(snaps)} snapshot(s), newest {snaps[-1]}")
