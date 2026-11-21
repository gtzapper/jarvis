import requests
import urllib3

from ..config import env
from ..models import Report, check_disk, check_usage, fmt_uptime, pct

IGNORE_FS = {"tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs", "vfat"}


class ProxmoxCollector:
    """Reads a Proxmox host or cluster through its REST API with a read-only token."""

    def __init__(self, cfg: dict, th: dict):
        self.name = cfg["name"]
        self.th = th
        self.must_run = {int(v) for v in cfg.get("must_run", [])}
        self.base = f"https://{cfg['host']}:{cfg.get('port', 8006)}/api2/json"
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

    def collect(self) -> list[Report]:
        return [self._node(node) for node in self._get("/nodes")]

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
        return rep

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
