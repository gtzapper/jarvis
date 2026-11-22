import json
from pathlib import Path

import requests
import urllib3

from ..config import env
from ..models import Report, check_disk, check_usage, fmt_uptime, pct

IGNORE_FS = {"tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs", "vfat"}
KNOWN_GUESTS = Path("data/known_guests.json")


class ProxmoxCollector:
    """Reads a Proxmox host or cluster through its REST API with a read-only token."""

    def __init__(self, cfg: dict, th: dict):
        self.name = cfg["name"]
        self.th = th
        self.must_run = {int(v) for v in cfg.get("must_run", [])}
        self.base = f"https://{cfg['host']}:{cfg.get('port', 8006)}/api2/json"
        self.token_env = cfg.get("token_id_env")
        token_id, secret = env(cfg.get("token_id_env")), env(cfg.get("token_secret_env"))
        if not token_id or not secret:
            raise ValueError(f"{self.name}: set {cfg.get('token_id_env')} and {cfg.get('token_secret_env')} in .env")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"PVEAPIToken={token_id}={secret}"
        self.s.verify = cfg.get("verify_ssl", False)
        if not self.s.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get(self, path: str):
        r = self.s.get(self.base + path, timeout=10)
        r.raise_for_status()
        return r.json()["data"]

    def permissions(self) -> dict:
        """What this token may do, e.g. {'/': {'Sys.Audit': 1, 'VM.Audit': 1}}. Empty when it has no rights at all."""
        try:
            return self._get("/access/permissions") or {}
        except (requests.RequestException, ValueError, KeyError):
            return None  # couldn't ask: assume it's fine rather than hide everything

    def can(self, priv: str, perms: dict | None = None) -> bool:
        perms = self.permissions() if perms is None else perms
        return perms is None or any(p.get(priv) for p in perms.values())

    def access_problem(self, perms: dict | None = None) -> str | None:
        """Why Jarvis can't see VMs/containers or node health, with the fix. None when the token has what it needs.
        Proxmox doesn't refuse a token without rights: it just returns empty lists, so check explicitly."""
        perms = self.permissions() if perms is None else perms
        if perms is None:
            return None
        missing = [p for p in ("VM.Audit", "Sys.Audit", "Datastore.Audit") if not self.can(p, perms)]
        if not missing:
            return None
        token = env(self.token_env) or "jarvis@pve!monitor"
        return (f"The Proxmox token has no {', '.join(missing)} permission, so Proxmox hides "
                + ("the VMs and containers" if "VM.Audit" in missing else "node health and storage")
                + f" from Jarvis. This usually means the token was made with 'Privilege Separation' ticked. Fix, in the Proxmox "
                  f"shell: pveum aclmod / -token '{token}' -role PVEAuditor  (or in the web UI: Datacenter > Permissions > Add > "
                  f"API Token Permission, path /, role PVEAuditor). Then press Test again.")

    def collect(self) -> list[Report]:
        nodes = self._get("/nodes")
        problem = self.access_problem()
        reps = [self._node(node) for node in nodes] if not problem or self.can("Sys.Audit") else []
        if problem:
            rep = reps[0] if reps else Report(self.name, "proxmox")
            rep.warn("permissions", problem)
            rep.lines.insert(0, "Can't see everything: " + problem.split(". This")[0])
            if not reps:
                reps = [rep]
        return reps

    def _node(self, node: dict) -> Report:
        n, th = node["node"], self.th
        rep = Report(n, "proxmox")
        if node.get("status") != "online":
            rep.reachable = False
            rep.crit("offline", f"Proxmox node {n} is {node.get('status', 'unknown')}")
            return rep

        st = self._get(f"/nodes/{n}/status")
        cpu = st["cpu"] * 100
        mem = pct(st["memory"]["used"], st["memory"]["total"])
        cores = st["cpuinfo"]["cpus"]
        load1 = float(st["loadavg"][0])
        rep.metrics.update(cpu=cpu, mem=mem, load1=load1, cores=cores, uptime=st["uptime"])
        rep.lines.append(f"CPU {cpu:.0f}%  RAM {mem:.0f}%  load {load1:.2f} ({cores} cores)  up {fmt_uptime(st['uptime'])}")
        check_usage(rep, th, cpu, mem, load1, cores)
        check_disk(rep, "root filesystem", pct(st["rootfs"]["used"], st["rootfs"]["total"]), th)

        for s in self._get(f"/nodes/{n}/storage"):
            if not s.get("enabled", 1):
                continue
            sid = s["storage"]
            if not s.get("active"):
                rep.warn(f"storage:{sid}", f"Storage {sid} on {n} is not available")
                rep.lines.append(f"Storage {sid}: INACTIVE")
                continue
            used = pct(s.get("used", 0), s.get("total", 0))
            rep.lines.append(f"Storage {sid}: {used:.0f}% used")
            check_disk(rep, f"storage {sid}", used, th)

        for d in self._get(f"/nodes/{n}/disks/list"):
            dev, health = d.get("devpath"), str(d.get("health", "UNKNOWN")).upper()
            if health not in ("PASSED", "OK", "UNKNOWN"):
                rep.crit(f"smart:{dev}", f"Disk {dev} {d.get('model', '')} on {n} reports SMART health {health}")
            # Proxmox's API reports wearout as life *remaining* (the web UI shows the inverse).
            wear = d.get("wearout")
            if isinstance(wear, (int, float)) and wear <= th["ssd_life_min_percent"]:
                rep.warn(f"wear:{dev}", f"SSD {dev} on {n} has {wear}% life left")

        guests = [("qemu", g) for g in self._get(f"/nodes/{n}/qemu")]
        guests += [("lxc", g) for g in self._get(f"/nodes/{n}/lxc")]
        guests = [(t, g) for t, g in guests if not g.get("template")]
        running = sum(1 for _, g in guests if g.get("status") == "running")
        rep.metrics["guests_running"] = running
        rep.lines.append(f"Guests: {running}/{len(guests)} running")
        for typ, g in sorted(guests, key=lambda tg: int(tg[1]["vmid"])):
            rep.lines.append(self._guest(rep, n, typ, g))
        self._new_guests(rep, n, guests)
        return rep

    def _new_guests(self, rep: Report, node: str, guests: list) -> None:
        """Announce VMs/LXCs that weren't there before. The first check only learns what exists."""
        try:
            known = json.loads(KNOWN_GUESTS.read_text(encoding="utf-8")) if KNOWN_GUESTS.exists() else None
        except ValueError:
            known = None
        key = f"{self.name}/{node}"  # per node, so a cluster's second node isn't all "new" on the first run
        first_run = known is None or key not in known
        known = known or {}
        seen = set(known.get(key, []))
        for typ, g in guests:
            if str(g["vmid"]) not in seen:
                seen.add(str(g["vmid"]))
                if not first_run:
                    kind = "VM" if typ == "qemu" else "container"
                    rep.events.append(f"New {kind} {g['vmid']} '{g.get('name', '?')}' appeared on {node}. "
                                      "Add it to Jarvis from Setup > Discovered on Proxmox")
        known[key] = sorted(seen, key=int)
        KNOWN_GUESTS.parent.mkdir(exist_ok=True)
        KNOWN_GUESTS.write_text(json.dumps(known), encoding="utf-8")

    def _guest(self, rep: Report, n: str, typ: str, g: dict) -> str:
        vmid = int(g["vmid"])
        label = f"{g.get('name', vmid)} ({vmid})"
        if g.get("status") != "running":
            if vmid in self.must_run:
                rep.crit(f"guest:{vmid}:down", f"{label} on {n} is {g.get('status')}")
            return f"  {label}: {g.get('status')}"

        line = f"  {label}: running, CPU {g.get('cpu', 0) * 100:.0f}%"
        if typ == "lxc":
            disk = pct(g.get("disk", 0), g.get("maxdisk", 0))
            mem = pct(g.get("mem", 0), g.get("maxmem", 0))
            if mem >= self.th["mem_percent"]:
                rep.warn(f"guest:{vmid}:mem", f"Container {label} memory is at {mem:.0f}%")
            check_disk(rep, f"container {label} disk", disk, self.th)
            return line + f", RAM {mem:.0f}%, disk {disk:.0f}%"

        filesystems = self._vm_filesystems(n, vmid)
        if filesystems is None:
            return line + ", no guest agent"
        for mount, used in filesystems.items():
            check_disk(rep, f"VM {label} {mount}", used, self.th)
        worst = max(filesystems.values(), default=0)
        return line + f", fullest disk {worst:.0f}%"

    def _vm_filesystems(self, n: str, vmid: int) -> dict | None:
        """Disk usage inside a VM via the QEMU guest agent. None if the agent isn't running or not permitted."""
        try:
            data = self._get(f"/nodes/{n}/qemu/{vmid}/agent/get-fsinfo")
        except requests.RequestException:
            return None
        out = {}
        for fs in data.get("result", []):
            total = fs.get("total-bytes")
            if not total or fs.get("type") in IGNORE_FS:
                continue
            out[fs.get("mountpoint", "?")] = pct(fs.get("used-bytes", 0), total)
        return out
