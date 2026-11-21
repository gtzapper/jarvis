"""One description of every service Jarvis can use: its fields, where its secrets live, and how to test it.
The terminal wizard and the web Setup page both work from this, so they always agree."""

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import dotenv_values

CONFIG = Path("config.yaml")
ENV = Path(".env")


@dataclass
class Field:
    key: str                 # key inside the config entry
    label: str
    kind: str = "text"       # text | secret | int | bool | list
    default: Any = None
    help: str = ""
    env: str = ""            # secrets: the .env variable that holds the value
    required: bool = False
    options: list | None = None  # kind 'choice': the allowed values


@dataclass
class Service:
    id: str
    label: str
    group: str
    path: tuple              # where it lives in config.yaml, e.g. ("media", "sonarr") or ("proxmox",)
    fields: list[Field]
    help: str = ""
    multi: bool = False      # a list of entries (proxmox, unraid, linux_hosts...) instead of one block
    test: Callable[[dict, dict], str] | None = field(default=None, repr=False)


# ---------------------------------------------------------------- connection tests
# Each gets (full effective config, this entry) and returns a short success message, or raises.

def _t_proxmox(cfg, e):
    from .collectors.proxmox import ProxmoxCollector
    v = ProxmoxCollector(e, cfg["thresholds"])._get("/version")
    return f"Connected to Proxmox {v.get('version', '?')}"


def _t_pfsense(cfg, e):
    from .collectors.pfsense import PfSenseCollector
    st = PfSenseCollector(e, cfg["thresholds"])._get("/status/system")
    return f"Connected to pfSense (CPU {st.get('cpu_usage', '?')}%)"


def _t_ssh(cfg, e):
    from .collectors.ssh_host import run_command
    code, out = run_command(e, "uname -sr && hostname", timeout=15)
    if code != 0:
        raise RuntimeError(out.strip() or f"exit {code}")
    return "SSH works: " + " / ".join(out.split())


def _t_media(cls_name):
    def test(cfg, e):
        from .media import clients, downloaders
        cls = getattr(clients, cls_name, None) or getattr(downloaders, cls_name)
        client = cls(e)
        return f"Connected to {client.name} {client.version()}"
    return test


def _t_provisioning(cfg, e):
    from .provision import Provisioner
    lines = Provisioner(cfg).check()
    fails = [l for l in lines if l.startswith("FAIL")]
    if fails:
        raise RuntimeError("; ".join(f[6:] for f in fails))
    return "All VM-creation checks passed"


def _t_notify(cfg, e):
    from .monitor import Notifier
    n = Notifier(cfg)
    if not n.ntfy_url:
        raise RuntimeError("No ntfy URL set")
    n.send("Jarvis", "Test notification from setup. Jarvis can reach you.")
    return "Test notification sent. Check your phone"


def _t_ai(cfg, e):
    from .brain import test_connection
    return test_connection(cfg)


def _key(env_name, label="API key", help=""):
    return Field("api_key_env", label, "secret", env=env_name, required=True, help=help)


def _url(port, required=True):
    return Field("url", "URL", default=f"http://192.168.1.20:{port}", required=required, help="Address you open it at in a browser")


def _profile():
    return [Field("quality_profile", "Quality profile", help="Name used when adding, e.g. HD-1080p. Blank = first profile"),
            Field("root_folder", "Root folder", help="e.g. /media/movies. Blank = first root folder")]


SSH_FIELDS = [
    Field("name", "Name", required=True, help="What you'll call it, e.g. docker-vm"),
    Field("host", "IP address", required=True),
    Field("user", "SSH user", default="jarvis"),
    Field("key_file", "SSH private key", default="~/.ssh/jarvis_ed25519", help="Generate one in the SSH key box if you don't have it"),
    Field("password_env", "Password (only if no key)", "secret", env="SSH_PASSWORD"),
    Field("must_run_containers", "Containers that must always run", "list", help="Comma separated, e.g. plex, nextcloud"),
]

SERVICES: list[Service] = [
    Service("proxmox", "Proxmox", "Infrastructure", ("proxmox",), multi=True, test=_t_proxmox,
            help="Read-only token. In the Proxmox shell run: pveum user add jarvis@pve; pveum aclmod / -user jarvis@pve -role PVEAuditor; "
                 "pveum user token add jarvis@pve monitor --privsep 0",
            fields=[Field("name", "Name", default="proxmox", required=True),
                    Field("host", "IP address", required=True), Field("port", "Port", "int", default=8006),
                    Field("token_id_env", "Token ID", "secret", env="PVE_TOKEN_ID", required=True, help="e.g. jarvis@pve!monitor"),
                    Field("token_secret_env", "Token secret", "secret", env="PVE_TOKEN_SECRET", required=True),
                    Field("verify_ssl", "Verify HTTPS certificate", "bool", default=False),
                    Field("must_run", "VM IDs that must always run", "list", help="e.g. 100, 101")]),
    Service("pfsense", "pfSense", "Infrastructure", ("pfsense",), multi=True, test=_t_pfsense,
            help="Install the REST API package, create a limited 'jarvis' user with GET privileges, then System > REST API > Keys as that user. See README step 3.",
            fields=[Field("name", "Name", default="pfsense", required=True),
                    Field("host", "IP address", default="192.168.1.1", required=True), Field("port", "Port", "int", default=443),
                    _key("PFSENSE_API_KEY"), Field("verify_ssl", "Verify HTTPS certificate", "bool", default=False),
                    Field("new_device_alerts", "Alert on new devices", "bool", default=True),
                    Field("ignore_interfaces", "Interfaces to ignore", "list", help="Enabled but unplugged ports, e.g. OPT2")]),
    Service("unraid", "Unraid", "Infrastructure", ("unraid",), multi=True, test=_t_ssh,
            help="Paste the Jarvis SSH public key into Unraid: Users > root > SSH authorized keys.",
            fields=[f if f.key != "user" else Field("user", "SSH user", default="root") for f in SSH_FIELDS]
            + [Field("allow_commands", "Allow 'run ... on' commands (as root)", "bool", default=False)]),
    Service("linux_hosts", "Linux VMs / servers", "Infrastructure", ("linux_hosts",), multi=True, test=_t_ssh,
            help="Create a 'jarvis' user on the VM and add the Jarvis SSH public key to ~jarvis/.ssh/authorized_keys.",
            fields=SSH_FIELDS + [Field("allow_commands", "Allow 'run ... on' commands", "bool", default=True),
                                 Field("command_timeout", "Command timeout (seconds)", "int", default=120)]),

    Service("plex", "Plex", "Media", ("media", "plex"), test=_t_media("PlexClient"),
            help="Plex Web > any movie > ... > Get Info > View XML. The address ends in X-Plex-Token=...",
            fields=[_url(32400), Field("token_env", "Plex token", "secret", env="PLEX_TOKEN", required=True)]),
    Service("sonarr", "Sonarr", "Media", ("media", "sonarr"), test=_t_media("SonarrClient"),
            help="Settings > General > API Key", fields=[_url(8989), _key("SONARR_API_KEY")] + _profile()),
    Service("radarr", "Radarr", "Media", ("media", "radarr"), test=_t_media("RadarrClient"),
            help="Settings > General > API Key", fields=[_url(7878), _key("RADARR_API_KEY")] + _profile()),
    Service("lidarr", "Lidarr", "Media", ("media", "lidarr"), test=_t_media("LidarrClient"),
            help="Settings > General > API Key", fields=[_url(8686), _key("LIDARR_API_KEY")] + _profile()),
    Service("prowlarr", "Prowlarr", "Media", ("media", "prowlarr"), test=_t_media("ProwlarrClient"),
            help="Settings > General > API Key", fields=[_url(9696), _key("PROWLARR_API_KEY")]),
    Service("overseerr", "Overseerr / Jellyseerr", "Media", ("media", "overseerr"), test=_t_media("OverseerrClient"),
            help="Settings > General > API Key", fields=[_url(5055), _key("OVERSEERR_API_KEY")]),

    Service("qbittorrent", "qBittorrent", "Downloads", ("media", "qbittorrent"), test=_t_media("QBittorrentClient"),
            help="Your Web UI login. Or whitelist Jarvis's IP in Web UI settings and leave the password blank.",
            fields=[_url(8080), Field("username", "Username", default="admin"),
                    Field("password_env", "Password", "secret", env="QBIT_PASSWORD")]),
    Service("deluge", "Deluge", "Downloads", ("media", "deluge"), test=_t_media("DelugeClient"),
            help="The web UI password (default: deluge).",
            fields=[_url(8112), Field("password_env", "Web UI password", "secret", env="DELUGE_PASSWORD"),
                    Field("slow_limit_kbps", "Slow mode cap (KB/s)", "int", default=1024)]),
    Service("sabnzbd", "SABnzbd", "Downloads", ("media", "sabnzbd"), test=_t_media("SabnzbdClient"),
            help="Config > General > API Key",
            fields=[_url(8085), _key("SABNZBD_API_KEY"), Field("slow_limit_percent", "Slow mode (% of line speed)", "int", default=25),
                    Field("min_free_gb", "Warn below free space (GB)", "int", default=20)]),

    Service("provisioning", "VM creation", "Automation", ("provisioning",), test=_t_provisioning,
            help="Needs a cloud-init template, a write-capable Proxmox token and a pfSense user that may write DHCP mappings. See README 'Creating VMs'.",
            fields=[Field("proxmox", "Proxmox entry name", default="proxmox", required=True),
                    Field("admin_token_id_env", "Write token ID", "secret", env="PVE_ADMIN_TOKEN_ID", required=True, help="e.g. jarvis-admin@pve!provision"),
                    Field("admin_token_secret_env", "Write token secret", "secret", env="PVE_ADMIN_TOKEN_SECRET", required=True),
                    Field("node", "Proxmox node", default="pve", required=True),
                    Field("template_vmid", "Template VM ID", "int", default=9000, required=True),
                    Field("storage", "Storage", default="local-lvm"), Field("bridge", "Bridge", default="vmbr0"),
                    Field("disk", "Disk", default="scsi0"), Field("ci_user", "User to create", default="jarvis"),
                    Field("ssh_public_key", "SSH public key", default="~/.ssh/jarvis_ed25519.pub"),
                    Field("pfsense", "pfSense entry name", default="pfsense", required=True),
                    Field("admin_api_key_env", "pfSense write API key", "secret", env="PFSENSE_ADMIN_API_KEY", required=True),
                    Field("dhcp_interface", "DHCP interface", default="lan"),
                    Field("ip_pool", "IP pool for new VMs", default="192.168.1.200-192.168.1.249", required=True,
                          help="Must be outside pfSense's dynamic DHCP range")]),
    Service("ai", "AI brain", "Automation", ("ai",), test=_t_ai,
            help="Lets you talk to Jarvis in plain English. 'claude' = Anthropic's cloud (smartest, pay per use, key from "
                 "console.anthropic.com). 'ollama' = a local model, fully offline and free, needs Ollama running on a machine "
                 "with a decent GPU. Exact commands like 'downloads' never use the AI.",
            fields=[Field("provider", "Provider", "choice", default="claude", options=["claude", "ollama"]),
                    Field("model", "Model", help="Blank = claude-opus-5-5 for Claude, qwen3:8b for Ollama"),
                    Field("api_key_env", "Anthropic API key (Claude only)", "secret", env="ANTHROPIC_API_KEY"),
                    Field("effort", "Effort (Claude only)", "choice", default="medium", options=["low", "medium", "high"],
                          help="low = faster and cheaper, high = most thorough"),
                    Field("ollama_url", "Ollama address (Ollama only)", default="http://localhost:11434",
                          help="e.g. http://192.168.1.20:11434 if Ollama runs on another machine"),
                    Field("think", "Thinking (Ollama only)", "choice", default="auto", options=["auto", "off", "on"],
                          help="off = faster replies on thinking models like qwen3.5; auto = the model's default"),
                    Field("num_ctx", "Context size (Ollama only)", "int", default=32768,
                          help="Tokens the model can see. Jarvis's tools need about 5k; lower it if you run out of GPU memory"),
                    Field("enabled", "Enabled", "bool", default=True)]),
    Service("notify", "Phone alerts (ntfy)", "Alerts", ("notify",), test=_t_notify,
            help="Install the ntfy app, subscribe to a long random topic, use https://ntfy.sh/<topic>.",
            fields=[Field("ntfy_url_env", "ntfy URL", "secret", env="NTFY_URL", required=True)]),
    Service("updates", "Updates", "Alerts", ("updates",),
            help="Jarvis updates itself from its GitHub repo (the button at the top of this page). For a private repo, create a "
                 "fine-grained GitHub token with read-only 'Contents' access to that one repo and paste it here.",
            fields=[Field("token_env", "GitHub token (private repos only)", "secret", env="JARVIS_GIT_TOKEN")]),
    Service("web", "Web dashboard", "Alerts", ("web",),
            help="Only needed to open the dashboard from other devices. Without a password it only listens on this PC.",
            fields=[Field("password_env", "Dashboard password", "secret", env="JARVIS_WEB_PASSWORD"),
                    Field("host", "Listen on", default="127.0.0.1", help="0.0.0.0 = whole network (password required)"),
                    Field("port", "Port", "int", default=8765)]),
]
BY_ID = {s.id: s for s in SERVICES}


# ---------------------------------------------------------------- reading

def load_raw() -> dict:
    return (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else None) or {}


def secrets() -> dict:
    return {**dotenv_values(ENV), **{k: v for k, v in os.environ.items() if k.isupper()}} if ENV.exists() else dict(os.environ)


def _get_path(raw: dict, path: tuple):
    node = raw
    for p in path:
        if not isinstance(node, dict) or p not in node:
            return None
        node = node[p]
    return node


def entries(svc: Service, raw: dict | None = None) -> list[dict]:
    value = _get_path(load_raw() if raw is None else raw, svc.path)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def env_name(svc: Service, f: Field, entry: dict, index: int) -> str:
    """Reuse the name already in config; otherwise PVE_TOKEN_SECRET for the first entry, PVE_TOKEN_SECRET_<NAME> after."""
    if entry.get(f.key):
        return entry[f.key]
    if index == 0 or not svc.multi:
        return f.env
    return f"{f.env}_{re.sub(r'[^A-Z0-9]', '_', str(entry.get('name', index)).upper())}"


def public_view(raw: dict | None = None) -> list[dict]:
    """Everything the Setup page needs. Secret values never leave this machine: only whether they're set."""
    raw = load_raw() if raw is None else raw
    sec = secrets()
    out = []
    for svc in SERVICES:
        items = []
        for i, e in enumerate(entries(svc, raw)):
            vals = {}
            for f in svc.fields:
                if f.kind == "secret":
                    vals[f.key] = {"set": bool(sec.get(env_name(svc, f, e, i)))}
                elif f.kind == "list":
                    vals[f.key] = ", ".join(map(str, e.get(f.key) or []))
                else:
                    vals[f.key] = e.get(f.key, f.default)
            items.append(vals)
        out.append({"id": svc.id, "label": svc.label, "group": svc.group, "help": svc.help, "multi": svc.multi,
                    "testable": svc.test is not None, "editable": not (not svc.multi and isinstance(_get_path(raw, svc.path), list)),
                    "fields": [{"key": f.key, "label": f.label, "kind": f.kind, "default": f.default, "help": f.help, "required": f.required,
                                "options": f.options}
                               for f in svc.fields],
                    "entries": items})
    return out


# ---------------------------------------------------------------- writing

def _write_raw(raw: dict) -> None:
    if CONFIG.exists():
        shutil.copy(CONFIG, CONFIG.with_suffix(".yaml.bak"))  # one step of undo
    CONFIG.write_text("# Written by Jarvis setup. A backup of the previous version is in config.yaml.bak\n"
                      + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def set_env(key: str, value: str) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    safe = value if re.fullmatch(r"[\w@%+=:,./!~-]*", value) else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    new = f"{key}={safe}"
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            lines[i] = new
            break
    else:
        lines.append(new)
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value  # take effect without a restart


def _coerce(f: Field, v):
    if f.kind == "int":
        return int(v) if str(v).strip() not in ("", "None") else f.default
    if f.kind == "bool":
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "y", "on")
    if f.kind == "choice":
        v = str(v or f.default).strip().lower()
        if f.options and v not in f.options:
            raise ValueError(f"{f.label} must be one of: {', '.join(f.options)}")
        return v
    if f.kind == "list":
        items = [str(x).strip() for x in (v if isinstance(v, list) else str(v or "").split(",")) if str(x).strip()]
        return [int(x) if str(x).isdigit() else x for x in items]
    return str(v).strip() if v is not None else ""


def save(service_id: str, values: dict, index: int | None = None) -> int:
    """Save one entry. Secrets: a non-empty value is written to .env, empty keeps the existing one.
    Returns the entry's index."""
    svc = BY_ID[service_id]
    raw = load_raw()
    current = entries(svc, raw)
    if not svc.multi and isinstance(_get_path(raw, svc.path), list):
        raise ValueError(f"{svc.label} has several instances in config.yaml. Edit that file by hand")
    if index is None or index >= len(current):
        index = len(current) if svc.multi else 0
        entry = {}
    else:
        entry = dict(current[index])

    missing = []
    for f in svc.fields:
        v = values.get(f.key)
        if f.kind == "secret":
            name = env_name(svc, f, {**entry, **({"name": values.get("name")} if "name" in values else {})}, index)
            if v:
                set_env(name, str(v))
                entry[f.key] = name
            elif secrets().get(name):
                entry[f.key] = name
            elif f.required:
                missing.append(f.label)
            else:
                entry.pop(f.key, None)
            continue
        v = _coerce(f, v if v is not None else entry.get(f.key, f.default))
        if f.required and v in ("", None, []):
            missing.append(f.label)
        if v in ("", None, []) and not f.required:
            entry.pop(f.key, None)
        else:
            entry[f.key] = v
    if missing:
        raise ValueError("Please fill in: " + ", ".join(missing))
    if svc.multi and any(i != index and e.get("name") == entry.get("name") for i, e in enumerate(current)):
        raise ValueError(f"There's already a {svc.label} entry called {entry.get('name')}")

    node = raw
    for p in svc.path[:-1]:
        node = node.setdefault(p, {})
    if svc.multi:
        lst = node.setdefault(svc.path[-1], [])
        if index < len(lst):
            lst[index] = entry
        else:
            lst.append(entry)
    else:
        node[svc.path[-1]] = entry
    _write_raw(raw)
    return index


def remove(service_id: str, index: int = 0) -> None:
    svc = BY_ID[service_id]
    raw = load_raw()
    node = _get_path(raw, svc.path[:-1]) if len(svc.path) > 1 else raw
    if not isinstance(node, dict) or svc.path[-1] not in node:
        return
    if svc.multi:
        lst = node[svc.path[-1]]
        if 0 <= index < len(lst):
            lst.pop(index)
        if not lst:
            node.pop(svc.path[-1])
    else:
        node.pop(svc.path[-1])
    _write_raw(raw)


def test(service_id: str, index: int = 0) -> str:
    """Test a saved entry against the real service. Raises with a readable message on failure."""
    from .config import load_config
    svc = BY_ID[service_id]
    if not svc.test:
        return "Nothing to test"
    cfg = load_config(allow_missing=True)
    items = entries(svc, load_raw())
    if index >= len(items):
        raise ValueError("Save it first")
    try:
        return svc.test(cfg, items[index])
    except Exception as e:
        msg = str(e)
        if "401" in msg or "403" in msg:
            msg += " (wrong key/password, or missing permission)"
        elif "timed out" in msg.lower() or "ConnectTimeout" in msg or "Max retries" in msg:
            msg = "Can't reach it. Check the IP/URL and port, and that it's running"
        raise RuntimeError(msg) from None


# ---------------------------------------------------------------- SSH key

def ensure_ssh_key(path: str = "~/.ssh/jarvis_ed25519") -> tuple[str, bool]:
    """Return (public key, newly_created). Creates an ed25519 key pair if missing."""
    priv = Path(path).expanduser()
    pub = priv.with_name(priv.name + ".pub")
    if priv.exists() and pub.exists():
        return pub.read_text(encoding="utf-8").strip(), False
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    priv.parent.mkdir(parents=True, exist_ok=True)
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
    try:
        os.chmod(priv, 0o600)
    except OSError:
        pass
    line = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode() + " jarvis"
    pub.write_text(line + "\n", encoding="utf-8")
    return line, True
