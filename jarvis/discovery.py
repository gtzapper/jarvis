"""Find every VM and LXC on Proxmox, work out its IP, and offer to add it to Jarvis as an SSH host.

IP sources, best first: the QEMU guest agent (VMs), Proxmox's own view (LXCs), a static IP in the guest's
config, and finally pfSense's device list matched by MAC address. Nothing is added without you clicking Add."""

import ipaddress
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from . import setup_schema as S
from .collectors.proxmox import ProxmoxCollector

MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
IP_RE = re.compile(r"\bip=(\d+\.\d+\.\d+\.\d+)")
WINDOWS = re.compile(r"^(win|w2k)", re.IGNORECASE)


def _usable(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("/")[0])
    except ValueError:
        return False
    return a.version == 4 and not (a.is_loopback or a.is_link_local or a.is_unspecified)


def _hostname(name: str, vmid: int) -> str:
    n = re.sub(r"[^a-z0-9-]+", "-", str(name or "").lower()).strip("-")
    return n or f"guest-{vmid}"


class Discovery:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._macs: dict[str, str] | None = None
        self._macs_lock = threading.Lock()  # guests are scanned in parallel; load pfSense's list once

    def _pfsense_macs(self) -> dict[str, str]:
        """MAC -> IP from pfSense's ARP table and DHCP leases (the fallback when a guest has no agent)."""
        with self._macs_lock:
            if self._macs is None:
                macs = {}
                from .collectors.pfsense import PfSenseCollector
                for e in self.cfg.get("pfsense") or []:
                    try:
                        for d in PfSenseCollector(e, self.cfg["thresholds"]).collect_devices():
                            if d.get("ip"):
                                macs[d["mac"].lower()] = d["ip"]
                    except Exception:
                        pass
                self._macs = macs
            return self._macs

    def _known(self) -> list[dict]:
        return list(self.cfg.get("linux_hosts") or []) + list(self.cfg.get("unraid") or [])

    def _guest(self, pve: ProxmoxCollector, source: str, node: str, kind: str, g: dict, must_run: set) -> dict:
        vmid = int(g["vmid"])
        out = {"source": source, "node": node, "type": "VM" if kind == "qemu" else "LXC", "vmid": vmid,
               "name": g.get("name") or str(vmid), "status": g.get("status", "?"), "ip": None, "ip_from": None,
               "os": "", "user": "root", "supported": True, "note": "", "must_run": vmid in must_run}
        try:
            conf = pve._get(f"/nodes/{node}/{kind}/{vmid}/config")
        except requests.RequestException:
            conf = {}
        out["os"] = conf.get("ostype", "")
        macs = [m.group(1).lower() for k, v in conf.items() if re.fullmatch(r"net\d+", k) for m in [MAC_RE.search(str(v))] if m]
        if kind == "qemu":
            out["user"] = conf.get("ciuser") or "root"
            if WINDOWS.match(out["os"]):
                out["supported"], out["note"] = False, "Windows: Jarvis's SSH checks are Linux-only"

        if g.get("status") == "running":
            try:
                if kind == "qemu":
                    ifaces = (pve._get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}).get("result", [])
                    # prefer the interface whose MAC Proxmox gave the VM; skip docker bridges and loopback
                    ifaces.sort(key=lambda i: str(i.get("hardware-address", "")).lower() not in macs)
                    for i in ifaces:
                        ip = next((a["ip-address"] for a in i.get("ip-addresses", [])
                                   if a.get("ip-address-type") == "ipv4" and _usable(a.get("ip-address", ""))), None)
                        if ip:
                            out["ip"], out["ip_from"] = ip, "guest agent"
                            break
                else:
                    for i in pve._get(f"/nodes/{node}/lxc/{vmid}/interfaces") or []:
                        ip = str(i.get("inet", "")).split("/")[0]
                        if i.get("name") != "lo" and _usable(ip):
                            out["ip"], out["ip_from"] = ip, "Proxmox"
                            break
            except requests.RequestException:
                pass
        if not out["ip"]:  # a static address written in the guest's config (LXC net0 ip=, cloud-init ipconfig0)
            m = next((IP_RE.search(str(v)) for k, v in conf.items() if re.fullmatch(r"(net|ipconfig)\d+", k) and IP_RE.search(str(v))), None)
            if m and _usable(m.group(1)):
                out["ip"], out["ip_from"] = m.group(1), "static config"
        if not out["ip"]:
            ip = next((self._pfsense_macs().get(mac) for mac in macs if self._pfsense_macs().get(mac)), None)
            if ip:
                out["ip"], out["ip_from"] = ip, "pfSense (by MAC)"
        if not out["ip"] and not out["note"]:
            out["note"] = "No IP found (stopped, no guest agent, or not seen by pfSense)"

        known = next((h for h in self._known() if (out["ip"] and h.get("host") == out["ip"])
                      or h.get("vmid") == vmid or h.get("name", "").lower() == out["name"].lower()), None)
        out["in_jarvis"] = known.get("name") if known else None
        out["suggested_name"] = _hostname(out["name"], vmid)
        return out

    def scan(self) -> list[dict]:
        entries = self.cfg.get("proxmox") or []
        if not entries:
            raise ValueError("Proxmox isn't set up in Jarvis yet")
        jobs = []
        for e in entries:
            pve = ProxmoxCollector(e, self.cfg["thresholds"])
            must_run = {int(v) for v in e.get("must_run") or []}
            for node in pve._get("/nodes"):
                if node.get("status") != "online":
                    continue
                n = node["node"]
                for kind in ("qemu", "lxc"):
                    for g in pve._get(f"/nodes/{n}/{kind}") or []:
                        if not g.get("template"):
                            jobs.append((pve, e["name"], n, kind, g, must_run))
        with ThreadPoolExecutor(max_workers=8) as ex:
            guests = list(ex.map(lambda j: self._guest(*j), jobs))
        return sorted(guests, key=lambda g: (g["in_jarvis"] is not None, g["vmid"]))


def add_guest(name: str, ip: str, user: str, key_file: str = "~/.ssh/jarvis_ed25519") -> tuple[int, str, bool]:
    """Add a discovered guest as a Linux host and try logging in. Returns (index, test message, ok)."""
    idx = S.save("linux_hosts", {"name": name, "host": ip, "user": user or "root", "key_file": key_file,
                                 "allow_commands": True, "command_timeout": 120})
    try:
        return idx, S.test("linux_hosts", idx), True
    except Exception as e:
        return idx, str(e), False


def set_must_run(source: str, vmid: int, on: bool) -> list[int]:
    """Tick or untick 'alert me if this guest stops' on a Proxmox entry."""
    entries = S.entries(S.BY_ID["proxmox"])
    idx = next((i for i, e in enumerate(entries) if e.get("name") == source), None)
    if idx is None:
        raise ValueError(f"No Proxmox entry called {source}")
    current = {int(v) for v in entries[idx].get("must_run") or []}
    current = (current | {vmid}) if on else (current - {vmid})
    S.save("proxmox", {"must_run": sorted(current)}, idx)
    return sorted(current)
