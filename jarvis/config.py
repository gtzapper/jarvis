import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULT_THRESHOLDS = {
    "cpu_percent": 90,
    "mem_percent": 90,
    "load_per_core": 2.0,
    "disk_percent": 85,
    "disk_critical_percent": 95,
    "disk_temp_c": 50,
    "ssd_life_min_percent": 10,
    "cpu_temp_c": 80,
    "state_table_percent": 80,
}


def load_config(path: str = "config.yaml", allow_missing: bool = False) -> dict:
    # Read .env from where Jarvis runs (/config in Docker), not from where the code lives.
    load_dotenv(Path.cwd() / ".env")
    p = Path(path)
    if not p.exists() and not allow_missing:
        raise SystemExit(f"{path} not found. Run 'python -m jarvis setup' (or 'python -m jarvis web' and open Setup) to create it.")
    cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {}
    cfg["thresholds"] = {**DEFAULT_THRESHOLDS, **(cfg.get("thresholds") or {})}
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("alert_after", 2)
    generated = Path("data/hosts.yaml")  # VMs Jarvis created itself
    if generated.exists():
        names = {h.get("name") for h in cfg.get("linux_hosts") or []}
        extra = [h for h in yaml.safe_load(generated.read_text(encoding="utf-8")) or [] if h.get("name") not in names]
        cfg["linux_hosts"] = (cfg.get("linux_hosts") or []) + extra
    return cfg


def env(name: str | None) -> str | None:
    """Secrets live in .env; config.yaml only names the variable."""
    return os.getenv(name) if name else None
