from ..media.clients import MediaStack
from ..models import Report
from .backups import BackupCollector, UnraidParityCollector
from .expiry import ExpiryCollector
from .media import MediaCollector
from .pfsense import PfSenseCollector
from .proxmox import ProxmoxCollector
from .ssh_host import SSHCollector


class Misconfigured:
    """Stands in for a collector whose config section is incomplete, so the rest still runs."""

    def __init__(self, name: str, kind: str, problem: str):
        self.name, self.kind, self.problem = name, kind, problem

    def collect(self) -> list[Report]:
        rep = Report(self.name, self.kind, reachable=False)
        rep.warn("config", f"{self.name} is skipped until its config is complete: {self.problem}")
        return [rep]


def _make(cls, c: dict, th: dict, label: str, **kw):
    try:
        return cls(c, th, **kw)
    except (ValueError, KeyError) as e:
        problem = str(e) if isinstance(e, ValueError) else f"missing '{e.args[0]}' in config.yaml"
        return Misconfigured(c.get("name", label), label, problem)


def build_collectors(cfg: dict, stack: MediaStack | None = None) -> list:
    th = cfg["thresholds"]
    collectors = [_make(ProxmoxCollector, c, th, "proxmox") for c in cfg.get("proxmox") or []]
    collectors += [_make(PfSenseCollector, c, th, "pfsense") for c in cfg.get("pfsense") or []]
    collectors += [_make(SSHCollector, c, th, "unraid", kind="unraid") for c in cfg.get("unraid") or []]
    collectors += [_make(SSHCollector, c, th, "linux", kind="linux") for c in cfg.get("linux_hosts") or []]
    watch = cfg.get("backup_watch") or {}
    if watch.get("enabled", True):
        collectors += [_make(BackupCollector, c, th, "backups", watch=watch) for c in cfg.get("proxmox") or []]
        collectors += [_make(UnraidParityCollector, c, th, "parity", watch=watch) for c in cfg.get("unraid") or []]
    sec = cfg.get("security_watch") or {}
    if sec.get("enabled", True):
        from ..security import LoginWatchCollector
        hosts = [h for h in (cfg.get("linux_hosts") or []) + (cfg.get("unraid") or []) if h.get("os") != "windows" and h.get("host")]
        collectors += [_make(LoginWatchCollector, h, th, "logins", watch=sec) for h in hosts]
    net = cfg.get("network_watch") or {}
    if net.get("enabled", True):
        from ..network import NetworkWatchCollector
        collectors.append(NetworkWatchCollector(net, th))
    exp = cfg.get("expiry_watch") or {}
    if exp.get("hosts") or exp.get("domains"):
        collectors.append(ExpiryCollector(exp, th))
    stack = stack if stack is not None else MediaStack(cfg)
    if stack:
        collectors.append(MediaCollector(stack, th))
    return collectors
