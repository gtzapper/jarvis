from dataclasses import dataclass, field


@dataclass
class Problem:
    key: str
    severity: str  # "warning" | "critical"
    message: str


@dataclass
class Report:
    """Everything Jarvis learned about one machine in one check."""

    name: str
    kind: str
    reachable: bool = True
    metrics: dict = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # one-off notices, e.g. "new device joined"

    def warn(self, key: str, message: str) -> None:
        self.problems.append(Problem(key, "warning", message))

    def crit(self, key: str, message: str) -> None:
        self.problems.append(Problem(key, "critical", message))


def pct(used, total) -> float:
    return (used / total * 100) if total else 0.0


def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h" if d else f"{h}h {rem // 60}m"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024


def check_usage(rep: Report, th: dict, cpu=None, mem=None, load1=None, cores=None) -> None:
    if cpu is not None and cpu >= th["cpu_percent"]:
        rep.warn("cpu", f"{rep.name} CPU is at {cpu:.0f}%")
    if mem is not None and mem >= th["mem_percent"]:
        rep.warn("mem", f"{rep.name} memory is at {mem:.0f}%")
    if load1 and cores and load1 / cores >= th["load_per_core"]:
        rep.warn("load", f"{rep.name} load is {load1:.1f} on {cores} cores")


def check_disk(rep: Report, label: str, used_pct: float, th: dict) -> None:
    key = f"disk:{label}"
    if used_pct >= th["disk_critical_percent"]:
        rep.crit(key, f"{rep.name} {label} is {used_pct:.0f}% full")
    elif used_pct >= th["disk_percent"]:
        rep.warn(key, f"{rep.name} {label} is {used_pct:.0f}% full")
