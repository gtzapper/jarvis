from datetime import datetime
from pathlib import Path

AUDIT_LOG = Path("data/audit.log")


def audit(event: str) -> None:
    """Every change Jarvis makes (commands run, VMs created, IPs reserved) is appended here."""
    AUDIT_LOG.parent.mkdir(exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {event}\n")
