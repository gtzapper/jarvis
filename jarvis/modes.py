"""Permission modes, like Claude Code's: how much Jarvis may do without asking first.

    ask        asks before every change
    auto       just does everyday changes and says what it did; anything destructive still asks
    read-only  looks but never changes anything, and says what it would do

Whatever the mode, destructive actions (reboots, deleting, wiping, stopping things) always ask."""

import json
import re
from pathlib import Path

MODES = {
    "ask": "Ask: I'll ask before every change.",
    "auto": "Auto: I'll just do everyday changes and tell you what I did. Anything destructive still asks first.",
    "read-only": "Read-only: I'll look but never change anything, and tell you what I would do.",
}
DEFAULT = "auto"
ALIASES = {"ask": "ask", "manual": "ask", "confirm": "ask", "careful": "ask",
           "auto": "auto", "automatic": "auto", "normal": "auto",
           "read-only": "read-only", "readonly": "read-only", "read only": "read-only", "plan": "read-only",
           "look only": "read-only", "dry run": "read-only", "dry-run": "read-only"}


def _file() -> Path:
    return Path("data") / "mode.json"


def normalise(word: str | None) -> str | None:
    return ALIASES.get(re.sub(r"\s+", " ", str(word or "")).strip().lower()) if word else None


def get(cfg: dict | None = None) -> str:
    try:
        mode = normalise(json.loads(_file().read_text()).get("mode"))
        if mode:
            return mode
    except (OSError, ValueError, AttributeError):
        pass
    return normalise(((cfg or {}).get("ai") or {}).get("mode")) or DEFAULT


def set_mode(mode: str) -> str:
    m = normalise(mode)
    if not m:
        raise ValueError(f"Unknown mode '{mode}'. Use ask, auto or read-only.")
    _file().parent.mkdir(exist_ok=True)
    _file().write_text(json.dumps({"mode": m}))
    return m


# ---------------------------------------------------------------- how risky is a shell command

RISKY = re.compile(
    r"\brm\s|\brmdir\b|\bmkfs|\bdd\s+if=|\b(shutdown|reboot|poweroff|halt)\b|>\s*/dev/(sd|nvme|vd|hd|mmc)|\bwipefs|\bfdisk|\bparted|\bsgdisk"
    r"|:\(\)\s*\{|\bchmod\s+-R|\bchown\s+-R|\buserdel|\bdeluser|\bpasswd\b|\bsystemctl\s+(stop|disable|mask|kill|isolate)"
    r"|\bdocker\s+(rm|rmi|kill|stop|system\s+prune|volume\s+(rm|prune)|network\s+(rm|prune)|image\s+(rm|prune)|container\s+(rm|prune|stop|kill))"
    r"|\bdocker[- ]compose\b.*\b(down|rm)\b|\bapt(-get)?\s+(remove|purge|autoremove)|\b(dnf|yum)\s+(remove|erase)|\bapk\s+del|\bpacman\s+-R"
    r"|\biptables\s+-(F|X|D)|\bnft\s+(flush|delete)|\bufw\s+(disable|reset|delete)|\bkill(all)?\b|\bpkill\b|\btruncate\b|\bshred\b"
    r"|\bzfs\s+(destroy|rollback)|\bzpool\s+(destroy|labelclear)|\blvremove|\bvgremove|\bpvremove|\b(qm|pct)\s+(destroy|stop|shutdown|reset)"
    r"|\bfind\b.*\s-delete\b|\bgit\s+(reset\s+--hard|clean\s+-\w*f|push\s+.*--force)|\bcrontab\s+-r|\|\s*(sudo\s+)?(ba|z)?sh\b|\bsed\s+-i\b|\bmv\s"
    r"|(^|[^0-9&2])>{1,2}\s*(?!/dev/null|&)[^\s|&;]"
    r"|Remove-Item|\b(Restart|Stop)-Computer\b|\bStop-(Service|Process|VM)\b|\bFormat-Volume\b|\bClear-Disk\b|\bInitialize-Disk\b"
    r"|\bdiskpart\b|\b(del|erase|rd|rmdir)\s|\bRemove-\w+|\bUninstall-\w+|\bDisable-\w+|\bSet-ExecutionPolicy\b|\bbcdedit\b|\bformat\s+[a-z]:",
    re.IGNORECASE)

# Commands that only look at things. In Auto and Read-only mode these run without asking.
_READ_WORDS = {
    "df", "du", "uptime", "free", "cat", "ls", "ll", "ps", "pstree", "top", "htop", "systemctl", "service", "journalctl", "dmesg",
    "docker", "ip", "ifconfig", "ping", "ss", "netstat", "uname", "hostname", "hostnamectl", "whoami", "id", "lsblk", "blkid", "mount",
    "findmnt", "grep", "egrep", "zgrep", "tail", "head", "less", "more", "wc", "sort", "uniq", "awk", "cut", "tr", "column", "find",
    "stat", "file", "which", "whereis", "type", "date", "timedatectl", "env", "printenv", "echo", "lscpu", "lspci", "lsusb", "lsof",
    "sensors", "smartctl", "zpool", "zfs", "pvs", "vgs", "lvs", "pvesh", "pveversion", "qm", "pct", "pvecm", "cat", "nproc", "vmstat",
    "iostat", "mpstat", "w", "who", "last", "dig", "nslookup", "host", "curl", "wget", "traceroute", "tracepath", "mtr", "arp",
    "resolvectl", "nmcli", "apt", "apt-cache", "dpkg", "rpm", "dnf", "yum", "apk", "pacman", "snap", "flatpak", "getent", "realpath",
    "readlink", "basename", "dirname", "test", "true", "sudo", "nvidia-smi", "lsmod", "modinfo", "ethtool", "iw", "nft", "iptables",
    "ufw", "fail2ban-client", "crontab", "systemd-analyze", "loginctl", "uptime", "tree", "jq", "md5sum", "sha256sum", "xargs",
}
_READ_SUB = {  # for these, only the listed sub-commands only read
    "systemctl": {"status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files", "list-timers", "show", "cat", "--failed"},
    "docker": {"ps", "logs", "inspect", "images", "stats", "info", "version", "top", "port", "df", "compose", "image", "container", "network", "volume"},
    "service": {"--status-all"}, "zfs": {"list", "get"}, "zpool": {"status", "list", "iostat"}, "qm": {"list", "config", "status"},
    "pct": {"list", "config", "status"}, "pvesh": {"get"}, "apt": {"list", "show", "policy", "search"}, "dnf": {"list", "info", "check-update", "search"},
    "yum": {"list", "info", "check-update", "search"}, "apk": {"info", "list", "search"}, "pacman": {"-Q", "-Qu", "-Ss", "-Si", "-Qi"},
    "snap": {"list", "info"}, "flatpak": {"list", "info"}, "nft": {"list"}, "iptables": {"-L", "-S", "-nL", "-nvL", "-vnL", "-Ln"},
    "ufw": {"status"}, "crontab": {"-l"}, "fail2ban-client": {"status"}, "dpkg": {"-l", "-L", "-s", "--list", "--get-selections"},
}
_DOCKER_SUB2 = {"ls", "ps", "logs", "config", "inspect", "images", "top", "port", "version"}
_PS_READ = re.compile(r"^(Get|Test|Measure|Select|Sort|Where|Format|Out|Resolve|Compare|Find|Search|Show)-\w+$|^(select|sort|where|ft|fl|gci|gc|gps|gsv|dir|ls|cat|type|hostname|whoami|ipconfig|ping|systeminfo|tasklist|netstat|nslookup|Write-Output|echo|%|\?|ForEach-Object|Where-Object)$", re.IGNORECASE)


def _segment_reads(seg: str, windows: bool) -> bool:
    words = seg.split()
    if not words:
        return True
    if windows:
        return bool(_PS_READ.match(words[0]))
    while words and (words[0] in ("sudo", "-n", "env", "nice", "timeout") or re.match(r"^\d+[smh]?$|^\w+=\S*$", words[0])):
        words = words[1:]
    if not words:
        return True
    cmd = words[0].rsplit("/", 1)[-1]
    if cmd not in _READ_WORDS:
        return False
    if cmd == "find" and re.search(r"\s-(delete|exec|execdir|ok|fprint)\b", seg):
        return False
    if cmd in ("curl", "wget") and re.search(r"\s(-X\s*(POST|PUT|DELETE|PATCH)|-d\b|--data|-O\b|-o\b|--output|-T\b|--upload)", seg, re.I):
        return False
    if cmd == "xargs" and not _segment_reads(" ".join(words[1:]), windows):
        return False
    subs = _READ_SUB.get(cmd)
    if subs is not None:
        args = [w for w in words[1:] if cmd in ("pacman", "iptables", "dpkg", "crontab") or not w.startswith("-")]
        sub = args[0] if args else ("--status-all" if cmd == "service" else "")
        if cmd == "systemctl" and not sub:
            return True  # bare systemctl lists units
        if cmd == "docker" and sub in ("compose", "image", "container", "network", "volume"):
            rest = [w for w in args[1:] if not w.startswith("-")]
            return bool(rest) and rest[0] in _DOCKER_SUB2
        return sub in subs
    return True


def classify(command: str, windows: bool = False) -> str:
    """'destructive', 'read' (only looks at things) or 'change'."""
    if RISKY.search(command):
        return "destructive"
    if re.search(r"`|\$\(|<\(", command):
        return "change"
    segments = re.split(r"\|\||&&|[|;\n]", command)
    return "read" if all(_segment_reads(s.strip(), windows) for s in segments) else "change"
