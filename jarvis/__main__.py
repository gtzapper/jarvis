import argparse
import json
import sys
from dataclasses import asdict

from .collectors import build_collectors
from .config import load_config
from .monitor import Notifier, collect_all, print_reports, run_monitor, spoken_summary


def main() -> None:
    # Torrent and show names are often non-ASCII; never let the Windows console crash on them.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="jarvis", description="Homelab monitoring assistant")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="check everything once and print a report")
    st.add_argument("--brief", action="store_true", help="one-sentence summary only")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("monitor", help="keep checking and alert on problems")
    dv = sub.add_parser("devices", help="list devices pfSense can see (IP, MAC, hostname)")
    dv.add_argument("search", nargs="?", help="filter by IP, MAC or hostname, e.g. 192.168.1.47")
    sub.add_parser("chat", help="talk to Jarvis with text commands")
    do = sub.add_parser("do", help='run one text command, e.g. jarvis do "add movie dune"')
    do.add_argument("text", nargs="+")
    do.add_argument("--yes", action="store_true", help="don't ask for confirmation; pick the top search result")
    sub.add_parser("test-notify", help="send a test push notification")
    up = sub.add_parser("update", help="update Jarvis from its git repo")
    up.add_argument("--check", action="store_true", help="only show what's new")
    up.add_argument("--yes", action="store_true", help="don't ask before updating")
    up.add_argument("--rollback", action="store_true", help="go back to the version before the last update")
    su = sub.add_parser("setup", help="guided setup: asks for each service and tests it")
    su.add_argument("service", nargs="?", help="set up just one, e.g. sonarr")
    wb = sub.add_parser("web", help="open the dashboard in your browser")
    wb.add_argument("--host", help="127.0.0.1 = this PC only (default), 0.0.0.0 = whole network (needs a password)")
    wb.add_argument("--port", type=int)
    wb.add_argument("--no-monitor", action="store_true", help="don't run background checks")
    args = ap.parse_args()

    if args.cmd == "update":
        run_update(args)
        return
    if args.cmd == "setup":
        from .wizard import run
        try:
            run(args.service)
        except KeyboardInterrupt:
            print("\nStopped. Everything you saved so far is kept.")
        return
    if args.cmd == "web":
        from .web.server import serve
        serve(args.host, args.port, monitor=not args.no_monitor)
        return

    cfg = load_config(args.config)
    if args.cmd == "test-notify":
        Notifier(cfg).send("Jarvis", "Test notification. Jarvis can reach you.")
        return

    if args.cmd == "chat":
        from .commands import chat
        chat(cfg)
        return
    if args.cmd == "do":
        from .commands import Assistant
        if args.yes:
            jarvis = Assistant(cfg, confirm=lambda q: True, choose=lambda options: 0)
        else:
            jarvis = Assistant(cfg)
        print(jarvis.handle(" ".join(args.text)))
        return

    if args.cmd == "devices":
        print_devices(cfg, args.search)
        return

    collectors = build_collectors(cfg)
    if not collectors:
        raise SystemExit("No hosts configured in config.yaml.")
    if args.cmd == "monitor":
        try:
            run_monitor(cfg, collectors)
        except KeyboardInterrupt:
            print("\nJarvis signing off.")
        return

    reports = collect_all(collectors)
    if args.json:
        print(json.dumps([asdict(r) for r in reports], indent=2))
    elif args.brief:
        print(spoken_summary(reports))
    else:
        print_reports(reports)


def run_update(args) -> None:
    from . import updater
    load_config(args.config, allow_missing=True)  # loads .env, for a private repo's token
    try:
        if args.rollback:
            print(updater.rollback()["message"], "Restart Jarvis to use it.")
            return
        st = updater.check()
        print(f"Installed: {st['commit']} ({st['date']}) {st['subject']}")
        if not st["new"]:
            print("Jarvis is up to date.")
            return
        print(f"{len(st['new'])} update(s) available on {st['upstream']}:")
        for c in st["new"]:
            print(f"  {c['commit']}  {c['date']}  {c['subject']}")
        if args.check:
            return
        if not args.yes and input("Update now? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Okay, not updating.")
            return
        result = updater.apply()
        print(result["message"], "Restart Jarvis to use the new version.")
    except updater.UpdateError as e:
        raise SystemExit(f"Update failed: {e}")


def print_devices(cfg: dict, search: str | None) -> None:
    from .collectors import PfSenseCollector

    sources = [PfSenseCollector(c, cfg["thresholds"]) for c in cfg.get("pfsense") or []]
    if not sources:
        raise SystemExit("No pfsense section in config.yaml.")
    for src in sources:
        devices = src.collect_devices()
        if search:
            q = search.lower()
            devices = [d for d in devices if q in d["ip"] or q in d["mac"] or q in d["hostname"].lower()]
        print(f"\n{src.name}: {len(devices)} device(s)")
        print(f"  {'IP':<16}{'MAC':<19}{'Interface':<12}{'Online':<8}Hostname")
        for d in devices:
            print(f"  {d['ip']:<16}{d['mac']:<19}{d['interface']:<12}{'yes' if d['online'] else 'no':<8}{d['hostname']}")


if __name__ == "__main__":
    main()
