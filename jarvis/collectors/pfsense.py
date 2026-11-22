import json
import re
import time
from collections import Counter
from pathlib import Path

import requests
import urllib3

from ..config import env
from ..models import Report, check_disk, check_usage, fmt_bytes, pct

KNOWN_DEVICES = Path("data/known_devices.json")
GATEWAY_BAD = {"down", "force_down"}
GATEWAY_DEGRADED = {"loss", "delay", "highloss", "highdelay"}


def num(v) -> float:
    """The API mostly returns numbers, but be tolerant of strings like '1.234ms' or '0.0%'."""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(\.\d+)?", str(v or ""))
    return float(m.group()) if m else 0.0


def is_random_mac(mac: str) -> bool:
    """Phones and laptops often use a private (randomised) MAC: the 'locally administered' bit is set."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except ValueError:
        return False


def parse_filterlog(line: str) -> dict | None:
    """Pull action/interface/source/destination port out of one pfSense filterlog line."""
    if "filterlog" not in line:
        return None
    f = line.split("filterlog", 1)[1].split(":", 1)[-1].strip().split(",")
    try:
        if f[8] == "4":
            src, proto, dport = f[18], f[16], f[21] if len(f) > 21 else ""
        elif f[8] == "6":
            src, proto, dport = f[15], f[12], f[18] if len(f) > 18 else ""
        else:
            return None
    except IndexError:
        return None
    return {"action": f[6], "iface": f[4], "src": src, "proto": proto, "dport": dport}


class PfSenseCollector:
    """Reads pfSense through the pfSense-pkg-RESTAPI package with a read-only API key."""

    kind = "pfsense"

    def __init__(self, cfg: dict, th: dict):
        self.name = cfg["name"]
        self.th = th
        self.new_device_alerts = cfg.get("new_device_alerts", True)
        self.log_lines = cfg.get("firewall_log_lines", 500)
        self.ignore_ifaces = {i.lower() for i in cfg.get("ignore_interfaces", [])}
        key = env(cfg.get("api_key_env"))
        if not key:
            raise ValueError(f"{self.name}: set {cfg.get('api_key_env')} in .env")
        self.base = f"https://{cfg['host']}:{cfg.get('port', 443)}/api/v2"
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = key
        self.s.verify = cfg.get("verify_ssl", False)
        if not self.s.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._last_counters: dict | None = None  # for throughput between checks

    def _get(self, path: str, **params):
        r = self.s.get(self.base + path, params=params, timeout=15)
        r.raise_for_status()
        return r.json()["data"]

    def _try(self, rep: Report, label: str, path: str, **params):
        """Optional sections: a missing privilege on one endpoint shouldn't hide everything else."""
        try:
            return self._get(path, **params)
        except requests.HTTPError as e:
            code = e.response.status_code
            hint = " (give the API user this GET privilege)" if code in (401, 403) else ""
            rep.lines.append(f"{label}: unavailable, HTTP {code}{hint}")
        except (requests.RequestException, ValueError, KeyError) as e:
            rep.lines.append(f"{label}: unavailable, {type(e).__name__}")
        return None

    def collect(self) -> list[Report]:
        rep = Report(self.name, "pfsense")
        self._system(rep)  # not wrapped: if this fails, pfSense is unreachable
        self._gateways(rep)
        self._interfaces(rep)
        self._states(rep)
        self._services(rep)
        self._devices(rep)
        self._firewall_log(rep)
        return [rep]

    def collect_devices(self) -> list[dict]:
        rep = Report(self.name, "pfsense")
        return self._device_table(rep)

    def _system(self, rep: Report) -> None:
        st = self._get("/status/system")
        cpu, mem, disk = num(st.get("cpu_usage")), num(st.get("mem_usage")), num(st.get("disk_usage"))
        temp = st.get("temp_c")
        rep.metrics.update(cpu=cpu, mem=mem, disk=disk, temp_c=temp)
        line = f"CPU {cpu:.0f}%  RAM {mem:.0f}%  disk {disk:.0f}%"
        if temp:
            line += f"  {num(temp):.0f}C"
            if num(temp) >= self.th["cpu_temp_c"]:
                rep.warn("temp", f"{rep.name} is running hot at {num(temp):.0f}C")
        if st.get("uptime"):
            line += f"  up {st['uptime']}"
        rep.lines.append(line)
        check_usage(rep, self.th, cpu, mem)
        check_disk(rep, "disk", disk, self.th)
        ver = self._try(rep, "Version", "/system/version")
        if ver:
            rep.lines.append(f"pfSense {ver.get('version', '?')}")

    def _gateways(self, rep: Report) -> None:
        for gw in self._try(rep, "Gateways", "/status/gateways") or []:
            name = gw.get("name", "?")
            status = str(gw.get("status", "")).lower()
            sub = str(gw.get("substatus", "")).lower()
            delay, loss = num(gw.get("delay")), num(gw.get("loss"))
            rep.lines.append(f"Gateway {name}: {status or 'unknown'}, {delay:.1f} ms, {loss:.0f}% loss")
            if status in GATEWAY_BAD:
                rep.crit(f"gw:{name}", f"Gateway {name} is down. Internet on that link is out")
            elif status in GATEWAY_DEGRADED or sub in GATEWAY_DEGRADED:
                rep.warn(f"gw:{name}", f"Gateway {name} is degraded: {delay:.0f} ms latency, {loss:.0f}% packet loss")

    def _interfaces(self, rep: Report) -> None:
        ifaces = self._try(rep, "Interfaces", "/status/interfaces") or []
        now = time.monotonic()
        counters = {}
        for i in ifaces:
            if not i.get("enable", True):
                continue
            label = i.get("descr") or i.get("name", "?")
            if label.lower() in self.ignore_ifaces or str(i.get("name", "")).lower() in self.ignore_ifaces:
                continue
            status = str(i.get("status", "")).lower()
            counters[label] = (num(i.get("inbytes")), num(i.get("outbytes")), num(i.get("inerrs")) + num(i.get("outerrs")))
            line = f"Interface {label} ({i.get('hwif', '?')}): {status}"
            if i.get("ipaddr"):
                line += f", {i['ipaddr']}"
            if self._last_counters and label in self._last_counters[1]:
                elapsed = now - self._last_counters[0]
                p_in, p_out, p_err = self._last_counters[1][label]
                c_in, c_out, c_err = counters[label]
                if elapsed > 0 and c_in >= p_in and c_out >= p_out:
                    down = (c_in - p_in) * 8 / elapsed / 1e6
                    up = (c_out - p_out) * 8 / elapsed / 1e6
                    line += f", in {down:.1f} Mbps / out {up:.1f} Mbps"
                    rep.metrics[f"mbps:{label}"] = (down, up)
                if c_err > p_err:
                    rep.warn(f"ifaceerr:{label}", f"Interface {label} on {rep.name} is logging errors ({int(c_err - p_err)} new)")
            else:
                line += f", {fmt_bytes(counters[label][0])} in / {fmt_bytes(counters[label][1])} out total"
            rep.lines.append(line)
            if status not in ("up", "associated"):
                rep.warn(f"iface:{label}", f"Interface {label} on {rep.name} is {status or 'not up'}")
        self._last_counters = (now, counters)

    def _states(self, rep: Report) -> None:
        sz = self._try(rep, "State table", "/firewall/states/size")
        if not sz:
            return
        cur, mx = num(sz.get("currentstates")), num(sz.get("maximumstates"))
        used = pct(cur, mx)
        rep.metrics["states"] = cur
        rep.lines.append(f"State table: {int(cur):,} / {int(mx):,} ({used:.0f}%)")
        if used >= self.th["state_table_percent"]:
            rep.warn("states", f"{rep.name} firewall state table is {used:.0f}% full")

    def _services(self, rep: Report) -> None:
        services = self._try(rep, "Services", "/status/services") or []
        stopped = [s for s in services if s.get("enabled") and not s.get("status")]
        if services:
            rep.lines.append(f"Services: {len(services) - len(stopped)}/{len(services)} running")
        for s in stopped:
            name = s.get("name", "?")
            rep.warn(f"svc:{name}", f"pfSense service {name} ({s.get('description', '')}) is stopped")

    def _device_table(self, rep: Report) -> list[dict]:
        """Merge the ARP table (works with any DHCP server) with DHCP leases (adds hostnames)."""
        devices: dict[str, dict] = {}
        for a in self._try(rep, "ARP table", "/diagnostics/arp_table") or []:
            mac = str(a.get("mac_address", "")).lower()
            if not mac or a.get("permanent") or "incomplete" in mac:
                continue  # permanent entries are pfSense's own interfaces
            host = a.get("hostname") if a.get("hostname") not in (None, "", "?") else ""
            devices[mac] = {"mac": mac, "ip": a.get("ip_address", ""), "hostname": host,
                            "interface": a.get("interface", ""), "online": True}
        for l in self._try(rep, "DHCP leases", "/status/dhcp_server/leases") or []:
            mac = str(l.get("mac", "")).lower()
            if not mac:
                continue
            d = devices.setdefault(mac, {"mac": mac, "ip": l.get("ip", ""), "hostname": "",
                                         "interface": l.get("if", ""), "online": False})
            d["hostname"] = d["hostname"] or l.get("hostname") or l.get("descr") or ""
            d["lease"] = l.get("active_status", "")
        return sorted(devices.values(), key=lambda d: [int(x) if x.isdigit() else 0 for x in d["ip"].split(".")])

    def _devices(self, rep: Report) -> None:
        devices = self._device_table(rep)
        online = sum(1 for d in devices if d["online"])
        rep.metrics["devices_online"] = online
        rep.lines.append(f"Devices: {online} online, {len(devices)} known to pfSense right now")
        if not self.new_device_alerts or not devices:
            return

        first_run = not KNOWN_DEVICES.exists()
        known = {} if first_run else json.loads(KNOWN_DEVICES.read_text(encoding="utf-8"))
        for d in devices:
            if d["mac"] in known:
                known[d["mac"]].update(ip=d["ip"], hostname=d["hostname"] or known[d["mac"]].get("hostname", ""))
                continue
            known[d["mac"]] = {"ip": d["ip"], "hostname": d["hostname"], "first_seen": time.strftime("%Y-%m-%d %H:%M")}
            if not first_run:
                who = d["hostname"] or "unnamed device"
                extra = ", private/random MAC (likely a phone or laptop)" if is_random_mac(d["mac"]) else ""
                msg = f"New device on {d['interface'] or 'the network'}: {who} at {d['ip']} ({d['mac']}){extra}. Say 'name device {d['ip']} as ...' to label it"
                rep.events.append(msg)
                from ..security import record_event
                record_event("new_device", self.name, msg)
        KNOWN_DEVICES.parent.mkdir(exist_ok=True)
        KNOWN_DEVICES.write_text(json.dumps(known, indent=2), encoding="utf-8")
        if first_run:
            rep.lines.append(f"Learned {len(devices)} existing devices. Only new ones will be announced from now on")

    def _firewall_log(self, rep: Report) -> None:
        entries = self._try(rep, "Firewall log", "/status/logs/firewall", limit=self.log_lines) or []
        blocks = [e for e in (parse_filterlog(x.get("text", "")) for x in entries) if e and e["action"] == "block"]
        if not entries:
            return
        rep.metrics["blocked_recent"] = len(blocks)
        line = f"Firewall: {len(blocks)} blocked in the last {len(entries)} log lines"
        if blocks:
            top_src = ", ".join(f"{ip} ({n})" for ip, n in Counter(b["src"] for b in blocks).most_common(3))
            top_port = ", ".join(f"{p}/{proto}" for (p, proto), _ in
                                 Counter((b["dport"], b["proto"]) for b in blocks if b["dport"]).most_common(3))
            line += f". Top sources: {top_src}"
            if top_port:
                line += f". Top ports: {top_port}"
        rep.lines.append(line)
