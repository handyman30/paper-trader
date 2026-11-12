import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

PAPER_URL = "https://paper-api.alpaca.markets"


def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["db_path"] = str(ROOT / cfg.get("db_path", "trader.db"))
    return cfg


def alpaca_keys() -> tuple[str | None, str | None]:
    return os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")


def has_keys() -> bool:
    k, s = alpaca_keys()
    return bool(k and s)


def assert_paper_only() -> None:
    """Refuse to run against anything but the paper endpoint."""
    url = os.getenv("ALPACA_BASE_URL", PAPER_URL).rstrip("/")
    if url != PAPER_URL:
        raise SystemExit(f"Refusing to run: ALPACA_BASE_URL is '{url}', only {PAPER_URL} is allowed.")


def with_claude_exits(cfg: dict) -> dict:
    """Same stocks and entries, Claude's exit rules (config.yaml -> claude_exits)."""
    import copy
    c = copy.deepcopy(cfg)
    for sleeve, over in (cfg.get("claude_exits") or {}).items():
        c["swing"][sleeve].update(over)
    return c
