"""Create VMs on Proxmox (cloned from a cloud-init template) and reserve their IP on pfSense.

Uses separate, write-capable credentials from the read-only monitoring ones."""

import ipaddress
import re
import socket
import time
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3
import yaml

from .audit import audit
from .config import env

GENERATED_HOSTS = Path("data/hosts.yaml")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5})")


class ProvisionError(Exception):
    pass


def _session(verify: bool) -> requests.Session:
    s = requests.Session()
    s.verify = verify
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


def _explain(r: requests.Response) -> str:
    try:
        body = r.json()
        detail = body.get("message") or body.get("errors") or body.get("data") or ""
    except ValueError:
        detail = r.text[:300]
    return f"HTTP {r.status_code} {r.reason}. {detail}".strip()


class ProxmoxAdmin:
    def __init__(self, pve_cfg: dict, prov: dict):
        token_id, secret = env(prov.get("admin_token_id_env")), env(prov.get("admin_token_secret_env"))
        if not token_id or not secret:
            raise ProvisionError(f"Set {prov.get('admin_token_id_env')} and {prov.get('admin_token_secret_env')} in .env (the write-capable Proxmox token)")
        self.base = f"https://{pve_cfg['host']}:{pve_cfg.get('port', 8006)}/api2/json"
        self.s = _session(pve_cfg.get("verify_ssl", False))
        self.s.headers["Authorization"] = f"PVEAPIToken={token_id}={secret}"

    def _req(self, method: str, path: str, **data):
        r = self.s.request(method, self.base + path, data=data or None, timeout=30)
        if not r.ok:
            raise ProvisionError(f"Proxmox {method} {path}: {_explain(r)}")
        return r.json().get("data")

    def next_id(self) -> int:
        return int(self._req("GET", "/cluster/nextid"))

    def vms(self) -> list[dict]:
        return self._req("GET", "/cluster/resources", type="vm") or []

    def config(self, node: str, vmid: int) -> dict:
        return self._req("GET", f"/nodes/{node}/qemu/{vmid}/config")

    def wait(self, node: str, upid: str, timeout: int = 900) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self._req("GET", f"/nodes/{node}/tasks/{quote(upid, safe='')}/status")
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise ProvisionError(f"Proxmox task failed: {st.get('exitstatus')}")
                return
            time.sleep(3)
        raise ProvisionError("Proxmox task took too long")

    def clone(self, node: str, template: int, newid: int, name: str, storage: str | None) -> None:
        params = {"newid": newid, "name": name, "full": 1}
        if storage:
            params["storage"] = storage
        self.wait(node, self._req("POST", f"/nodes/{node}/qemu/{template}/clone", **params))

    def set_config(self, node: str, vmid: int, **params) -> None:
        self._req("PUT", f"/nodes/{node}/qemu/{vmid}/config", **params)

    def resize(self, node: str, vmid: int, disk: str, size_gb: int) -> None:
        self._req("PUT", f"/nodes/{node}/qemu/{vmid}/resize", disk=disk, size=f"{size_gb}G")

    def start(self, node: str, vmid: int) -> None:
        self.wait(node, self._req("POST", f"/nodes/{node}/qemu/{vmid}/status/start"))


class PfSenseAdmin:
    def __init__(self, pf_cfg: dict, prov: dict):
        key = env(prov.get("admin_api_key_env"))
        if not key:
            raise ProvisionError(f"Set {prov.get('admin_api_key_env')} in .env (the pfSense key that may write DHCP mappings)")
        self.base = f"https://{pf_cfg['host']}:{pf_cfg.get('port', 443)}/api/v2"
        self.s = _session(pf_cfg.get("verify_ssl", False))
        self.s.headers["X-API-Key"] = key

    def _req(self, method: str, path: str, body: dict | None = None, **params):
        r = self.s.request(method, self.base + path, json=body, params=params or None, timeout=30)
        if not r.ok:
            raise ProvisionError(f"pfSense {method} {path}: {_explain(r)}")
        return r.json().get("data")

    def dhcp_server(self, iface: str) -> dict:
        for d in self._req("GET", "/services/dhcp_servers") or []:
            if iface in (d.get("id"), d.get("interface")):
                return d
        raise ProvisionError(f"pfSense has no DHCP server on interface '{iface}'")

    def used_ips(self, iface: str, server: dict) -> set[str]:
        used = {m.get("ipaddr") for m in server.get("staticmap") or []}
        try:
            maps = self._req("GET", "/services/dhcp_server/static_mappings", parent_id=iface) or []
            used |= {m.get("ipaddr") for m in maps}
        except ProvisionError:
            pass  # the embedded list above is enough if this endpoint isn't permitted
        try:
            used |= {a.get("ip_address") for a in self._req("GET", "/diagnostics/arp_table") or []}
        except ProvisionError:
            pass
        return {u for u in used if u}

    def reserve(self, iface: str, mac: str, ip: str, hostname: str) -> None:
        self._req("POST", "/services/dhcp_server/static_mapping", {
            "parent_id": iface, "mac": mac.lower(), "ipaddr": ip, "hostname": hostname,
            "descr": f"Created by Jarvis {time.strftime('%Y-%m-%d')}"})
        self._req("POST", "/services/dhcp_server/apply", {})


def pick_ip(pool: str, used: set[str], range_from: str | None, range_to: str | None) -> str:
    start, end = (ipaddress.ip_address(p.strip()) for p in pool.split("-"))
    dyn = (ipaddress.ip_address(range_from), ipaddress.ip_address(range_to)) if range_from and range_to else None
    ip = start
    while ip <= end:
        if str(ip) not in used and not (dyn and dyn[0] <= ip <= dyn[1]):
            return str(ip)
        ip += 1
    raise ProvisionError(f"No free addresses left in {pool}")


def wait_for_ssh(ip: str, timeout: int = 240) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((ip, 22), timeout=3):
                return True
        except OSError:
            time.sleep(5)
    return False


def register_host(cfg: dict, entry: dict) -> None:
    hosts = yaml.safe_load(GENERATED_HOSTS.read_text(encoding="utf-8")) if GENERATED_HOSTS.exists() else []
    hosts = [h for h in hosts or [] if h.get("name") != entry["name"]] + [entry]
    GENERATED_HOSTS.parent.mkdir(exist_ok=True)
    GENERATED_HOSTS.write_text(yaml.safe_dump(hosts, sort_keys=False), encoding="utf-8")
    cfg["linux_hosts"] = [h for h in cfg.get("linux_hosts") or [] if h.get("name") != entry["name"]] + [entry]


class Provisioner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.p = cfg.get("provisioning")
        if not self.p:
            raise ProvisionError("Add a 'provisioning' section to config.yaml first (see config.example.yaml)")
        pve = next((x for x in cfg.get("proxmox") or [] if x["name"] == self.p.get("proxmox")), None)
        pf = next((x for x in cfg.get("pfsense") or [] if x["name"] == self.p.get("pfsense")), None)
        if not pve or not pf:
            raise ProvisionError("provisioning.proxmox and provisioning.pfsense must name entries in the proxmox/pfsense sections")
        self.pve = ProxmoxAdmin(pve, self.p)
        self.pf = PfSenseAdmin(pf, self.p)
        self.node = self.p["node"]
        self.iface = self.p.get("dhcp_interface", "lan")

    def _public_key(self) -> str:
        path = Path(self.p.get("ssh_public_key", "~/.ssh/jarvis_ed25519.pub")).expanduser()
        if not path.exists():
            raise ProvisionError(f"SSH public key {path} not found")
        return path.read_text(encoding="utf-8").strip()

    def plan(self, name: str, cores: int | None, memory_mb: int | None, disk_gb: int | None) -> dict:
        name = name.lower()
        if not NAME_RE.match(name):
            raise ProvisionError(f"'{name}' isn't a valid hostname. Use letters, digits and dashes")
        d, lim = self.p.get("defaults") or {}, self.p.get("limits") or {}
        spec = {"cores": cores or d.get("cores", 2), "memory_mb": memory_mb or d.get("memory_mb", 2048),
                "disk_gb": disk_gb or d.get("disk_gb", 32)}
        for k, v in spec.items():
            if lim.get(k) and v > lim[k]:
                raise ProvisionError(f"{k.replace('_', ' ')} {v} is over your configured limit of {lim[k]}")
        if any(v.get("name") == name for v in self.pve.vms()):
            raise ProvisionError(f"A VM called {name} already exists in Proxmox")
        if any(h.get("name", "").lower() == name for h in self.cfg.get("linux_hosts") or []):
            raise ProvisionError(f"Jarvis already has a host called {name}")
        self._public_key()
        server = self.pf.dhcp_server(self.iface)
        ip = pick_ip(self.p["ip_pool"], self.pf.used_ips(self.iface, server), server.get("range_from"), server.get("range_to"))
        return {"name": name, "vmid": self.pve.next_id(), "ip": ip, **spec}

    def describe(self, plan: dict) -> str:
        return (f"Create VM {plan['vmid']} '{plan['name']}' on {self.node}: {plan['cores']} cores, "
                f"{plan['memory_mb'] // 1024 if plan['memory_mb'] >= 1024 else plan['memory_mb']}"
                f"{' GB' if plan['memory_mb'] >= 1024 else ' MB'} RAM, {plan['disk_gb']} GB disk, "
                f"cloned from template {self.p['template_vmid']}. Reserve {plan['ip']} for it on pfSense ({self.iface}), then start it?")

    def execute(self, plan: dict, say) -> dict:
        name, vmid, ip = plan["name"], plan["vmid"], plan["ip"]
        say(f"Cloning template {self.p['template_vmid']} to VM {vmid}...")
        self.pve.clone(self.node, self.p["template_vmid"], vmid, name, self.p.get("storage"))
        audit(f"proxmox: cloned template {self.p['template_vmid']} -> VM {vmid} '{name}'")

        m = MAC_RE.search(self.pve.config(self.node, vmid).get("net0", ""))
        if not m:
            raise ProvisionError(f"VM {vmid} was created, but I couldn't read its MAC address. It's stopped; check its network device")
        mac = m.group(1).upper()
        say(f"Configuring: {plan['cores']} cores, {plan['memory_mb']} MB RAM, user {self.p.get('ci_user', 'jarvis')}, MAC {mac}")
        self.pve.set_config(
            self.node, vmid, cores=plan["cores"], memory=plan["memory_mb"],
            net0=f"virtio={mac},bridge={self.p.get('bridge', 'vmbr0')}",
            ciuser=self.p.get("ci_user", "jarvis"),
            sshkeys=quote(self._public_key(), safe=""),  # Proxmox wants this one URL-encoded
            ipconfig0="ip=dhcp", onboot=1, agent="enabled=1",
            description=f"Created by Jarvis {time.strftime('%Y-%m-%d %H:%M')}")
        try:
            self.pve.resize(self.node, vmid, self.p.get("disk", "scsi0"), plan["disk_gb"])
        except ProvisionError as e:
            say(f"Couldn't resize the disk ({e}). Continuing with the template's size")

        say(f"Reserving {ip} for {name} on pfSense...")
        try:
            self.pf.reserve(self.iface, mac, ip, name)
        except ProvisionError as e:
            raise ProvisionError(f"VM {vmid} exists but is NOT started, because the pfSense reservation failed: {e}. "
                                 f"Add a static mapping for {mac} -> {ip} by hand and start it, or delete VM {vmid}")
        audit(f"pfsense: reserved {ip} for {name} ({mac}) on {self.iface}")

        say("Starting the VM...")
        self.pve.start(self.node, vmid)
        audit(f"proxmox: started VM {vmid} '{name}'")
        say(f"Waiting for {name} to boot and answer on {ip}:22 (cloud-init takes a minute or two)...")
        up = wait_for_ssh(ip)

        key = Path(self.p.get("ssh_public_key", "~/.ssh/jarvis_ed25519.pub")).with_suffix("").as_posix()
        register_host(self.cfg, {"name": name, "host": ip, "user": self.p.get("ci_user", "jarvis"),
                                 "key_file": key, "vmid": vmid, "allow_commands": True})
        return {**plan, "mac": mac, "ssh_up": up}

    def check(self) -> list[str]:
        """Dry run: confirm every permission and setting works, without changing anything."""
        out = []

        def step(label, fn):
            try:
                out.append(f"OK    {label}: {fn() or 'fine'}")
            except Exception as e:
                out.append(f"FAIL  {label}: {e}")

        def template():
            c = self.pve.config(self.node, self.p["template_vmid"])
            if not c.get("template"):
                raise ProvisionError(f"VM {self.p['template_vmid']} isn't marked as a template (run: qm template {self.p['template_vmid']})")
            if not any("cloudinit" in str(v) for v in c.values()):
                raise ProvisionError("template has no cloud-init drive")
            if not MAC_RE.search(c.get("net0", "")):
                raise ProvisionError("template has no net0 network device")
            return f"VM {self.p['template_vmid']} '{c.get('name')}' is a cloud-init template"

        def dhcp():
            s = self.pf.dhcp_server(self.iface)
            used = self.pf.used_ips(self.iface, s)
            ip = pick_ip(self.p["ip_pool"], used, s.get("range_from"), s.get("range_to"))
            return f"dynamic range {s.get('range_from')}-{s.get('range_to')}, {len(used)} addresses in use, next free in pool: {ip}"

        step("Proxmox write token", lambda: f"next free VM ID is {self.pve.next_id()}")
        step("Template", template)
        step("SSH public key", lambda: self._public_key()[:30] + "...")
        step("pfSense DHCP", dhcp)
        out.append("Note: the pfSense write permission is only proven by a real create. If that step fails, the VM is left stopped and nothing else changes.")
        return out
