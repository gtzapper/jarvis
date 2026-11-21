"""Windows hosts: same SSH key, but PowerShell instead of a Linux shell script.

Needs OpenSSH Server on the Windows machine (a built-in optional feature on Windows 10/11 and Server 2019+)."""

import base64
import json
import time

from ..models import Report, check_disk, check_usage, fmt_bytes, fmt_uptime, pct

UPDATE_CHECK_EVERY = 6 * 3600   # searching Windows Update is slow, so only every few hours
OK_EXIT_CODES = {0, 1077}        # 1077 = "not started since boot": trigger/delayed-start services, not failures
# Automatic services that routinely stop themselves when they have nothing to do.
SELF_STOPPING = {"mapsbroker", "edgeupdate", "edgeupdatem", "gupdate", "gupdatem", "sppsvc", "remoteregistry",
                 "tiledatamodelsvc", "cdpsvc", "wbiosrvc", "trustedinstaller", "clicktorunsvc", "sysmain"}

SCRIPT = r"""[Console]::OutputEncoding=[Text.Encoding]::UTF8
$ErrorActionPreference='SilentlyContinue'
$o=Get-CimInstance Win32_OperatingSystem
$r=[ordered]@{
 cpu=(Get-CimInstance Win32_Processor|Measure-Object LoadPercentage -Average).Average
 cores=(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
 memTotal=$o.TotalVisibleMemorySize; memFree=$o.FreePhysicalMemory
 uptime=[int]((Get-Date)-$o.LastBootUpTime).TotalSeconds; os=$o.Caption
 disks=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'|%{@{n=$_.DeviceID;s=$_.Size;f=$_.FreeSpace}})
 svc=@(Get-CimInstance Win32_Service -Filter "StartMode='Auto' AND State<>'Running'"|%{@{n=$_.Name;d=$_.DisplayName;e=$_.ExitCode}})
 reboot=(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
 docker=$null; updates=$null
}
if(Get-Command docker){$r.docker=@(docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}')}
__UPDATES__
$r|ConvertTo-Json -Compress -Depth 4
"""
UPDATES = r"""try{$r.updates=(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'").Updates.Count}catch{}"""


def ps_wrap(command: str) -> str:
    """Run PowerShell regardless of the account's default SSH shell (cmd.exe or PowerShell)."""
    script = "$ProgressPreference='SilentlyContinue'\n" + command
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    return f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def script(with_updates: bool) -> str:
    return SCRIPT.replace("__UPDATES__", UPDATES if with_updates else "")


def _list(v) -> list:
    """PowerShell turns a one-item array into a plain object (or null); always give a list back."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def parse(output: str) -> dict:
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end < start:
        raise ValueError("No data came back from PowerShell: " + output.strip()[:200])
    return json.loads(output[start:end + 1])


class WindowsState:
    """Remembers the slow Windows Update count between checks."""

    def __init__(self):
        self.updates: int | None = None
        self.checked = 0.0

    def due(self) -> bool:
        return time.time() - self.checked > UPDATE_CHECK_EVERY

    def remember(self, count) -> None:
        self.checked = time.time()
        if count is not None:
            self.updates = int(count)


def report(rep: Report, data: dict, th: dict, ignore_services: set, state: WindowsState, checked_updates: bool) -> list[str]:
    """Fill the report. Returns Docker rows (name\\tstate\\tstatus) so the Linux container checks can be reused."""
    cpu = float(data.get("cpu") or 0)
    total, free = float(data.get("memTotal") or 0), float(data.get("memFree") or 0)
    mem = pct(total - free, total)
    uptime = int(data.get("uptime") or 0)
    rep.metrics.update(cpu=cpu, mem=mem, cores=data.get("cores"), uptime=uptime)
    rep.lines.append(f"{data.get('os') or 'Windows'}")
    rep.lines.append(f"CPU {cpu:.0f}%  RAM {mem:.0f}% of {fmt_bytes(total * 1024)}  ({data.get('cores') or '?'} cores)  up {fmt_uptime(uptime)}")
    check_usage(rep, th, cpu, mem)

    for d in _list(data.get("disks")):
        size, left = float(d.get("s") or 0), float(d.get("f") or 0)
        if not size:
            continue
        used = pct(size - left, size)
        rep.lines.append(f"Drive {d.get('n')} {used:.0f}% used ({fmt_bytes(left)} free)")
        check_disk(rep, f"drive {d.get('n', '?')}", used, th)

    for s in _list(data.get("svc")):
        name = s.get("n", "?")
        if s.get("e") in OK_EXIT_CODES or name.lower() in ignore_services or name.lower() in SELF_STOPPING:
            continue
        rep.warn(f"winsvc:{name}", f"Service {s.get('d') or name} stopped unexpectedly on {rep.name} (exit code {s.get('e')})")

    if checked_updates:
        state.remember(data.get("updates"))
    if state.updates:
        rep.lines.append(f"{state.updates} Windows update(s) waiting")
    if data.get("reboot"):
        rep.lines.append("Restart pending to finish installing updates")

    rows = []
    for line in _list(data.get("docker")):
        parts = str(line).split("|", 2)
        if len(parts) == 3:
            rows.append("\t".join(parts))
    return rows


SETUP_SNIPPET = """# Run in PowerShell as Administrator on the Windows machine
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' -Protocol TCP -LocalPort 22 -Action Allow -ErrorAction SilentlyContinue
Add-Content -Path "$env:ProgramData\\ssh\\administrators_authorized_keys" -Value '__KEY__'
icacls "$env:ProgramData\\ssh\\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
"""
