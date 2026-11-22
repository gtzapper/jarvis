"""Text commands. Each handler is a plain function, so the future AI brain can call the same ones
as tools when you speak in free-form English."""

import re
from typing import Callable

import requests

from .media.clients import MediaStack, PlexClient
from .media.downloaders import fmt_speed

from . import modes
from .modes import RISKY  # noqa: F401  (kept for anything importing it from here)

KINDS = {
    "movie": "movie", "movies": "movie", "film": "movie", "films": "movie",
    "show": "show", "shows": "show", "series": "show", "tv": "show", "episodes": "show",
    "artist": "artist", "band": "artist", "music": "artist", "albums": "artist",
}
KIND_WORDS = "|".join(sorted(KINDS, key=len, reverse=True))

HELP = """Things I understand:
  status                         how is everything (the whole lab)
  media status                   just the media stack
  downloads / queue              what's downloading, numbered
  remove download 2              drop a stuck download and blocklist that release
  torrents / speeds              download clients (qBittorrent, Deluge, SABnzbd), numbered
  pause downloads / resume downloads   everything (or: pause qbittorrent, resume sabnzbd)
  pause 3 / resume 3             one item from the last 'torrents' list
  slow mode on / slow mode off   throttle downloads (e.g. while gaming or streaming)
  playing / who's watching       current Plex streams
  recent                         recently added to Plex
  upcoming [14 days]             episodes, movies and albums coming soon
  search movie dune              look something up (also: show, artist)
  add movie dune                 add it and start searching (also: show, artist)
  search missing movies          ask Radarr/Sonarr/Lidarr to hunt for missing items
  requests                       pending Overseerr requests, numbered
  approve 2 / decline 2 / approve all
  refresh plex [library name]    scan libraries for new files
  indexers / test indexers       Prowlarr indexer health
  hosts                          machines I can run commands on
  scan proxmox                   find every VM/LXC, its IP, and whether Jarvis can log in
  run uptime on web1             run a shell command on a host (always asks first)
  create vm web1 [with 4 cores 8gb ram 64gb disk]   new VM from your template + static IP on pfSense
  check vm setup                 test the VM-creation permissions without changing anything
  check for updates / update jarvis   see what's new on GitHub, install it and restart
  check updates on docker-vm     OS packages and container images waiting (or: on all)
  update docker-vm               install OS updates and new container images (asks first)
  update packages on X / update containers on X / update plex container on unraid
  update everything              every machine Jarvis may run commands on
  restart sonarr on docker-vm    restart one container
  clean up docker on X           remove unused images and build cache
  reboot docker-vm               restart the machine (always asks)
  auto mode / ask mode / read-only mode   how much I do without asking (see 'mode')
  new chat                       start a fresh AI conversation
  help                           this list
With the AI brain set up, just say what you want in plain English: I work out the steps, retry when
something fails, and tell you what I'm doing as I go."""


def ask_yes(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def ask_choice(options: list[str]) -> int | None:
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    pick = input(f"Which one? (1-{len(options)}, Enter to cancel) ").strip()
    return int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(options) else None


class Assistant:
    def __init__(self, cfg: dict, stack: MediaStack | None = None,
                 confirm: Callable[[str], bool] = ask_yes,
                 choose: Callable[[list[str]], int | None] = ask_choice,
                 say: Callable[[str], None] = print):
        self.cfg = cfg
        self.stack = stack if stack is not None else MediaStack(cfg)
        self.confirm = confirm
        self.choose = choose
        self.say = say  # progress updates during long jobs
        self.brain = None
        from .brain import available, make_brain
        if available(cfg):
            self.brain = make_brain(cfg, self)
        self.last_queue: list[tuple] = []     # (client, item) from the last "downloads"
        self.last_requests: list[dict] = []   # from the last "requests"
        self.last_items: list[tuple] = []     # (client, item) from the last "torrents"
        self.rules: list[tuple[re.Pattern, Callable]] = [
            (r"^(help|\?|what can you do)$", self.help),
            (r"^(new chat|new conversation|reset|forget (it|that|everything))$", self.new_chat),
            (r"^((switch|change|go|set)( to| mode( to)?)? )?(?P<mode>ask|manual|careful|auto|automatic|normal|read[- ]?only|plan|look only|dry[- ]run)( mode)?$", self.set_mode),
            (r"^(mode|what mode|which mode|what mode are you in|permission mode)$", self.set_mode),
            (r"^(hosts|list hosts|machines|servers)$", self.hosts),
            (r"^(scan proxmox|discover( vms| guests)?|find (my )?(vms|guests|containers))$", self.discover),
            (r"^(run|exec|execute) (?P<cmd>.+?) on (?P<host>[\w.-]+)$", self.run),
            (r"^on (?P<host>[\w.-]+)[,:]? (run |exec |execute )?(?P<cmd>.+)$", self.run),
            (r"^check (vm|provisioning) setup$", self.check_vm_setup),
            (r"^(check for updates?|any updates|what version( are you)?|version)$", self.check_updates),
            (r"^(update|upgrade) (jarvis|yourself|the app)$", self.update_self),
            (r"^check (for )?updates (on|for) (?P<host>all( (machines|hosts|servers))?|everything|[\w.-]+)$", self.host_updates_check),
            (r"^(update|upgrade) (everything|all( (machines|hosts|servers))?)$", self.update_everything),
            (r"^(update|upgrade) (?P<what>packages|the os|os|system|containers|all containers|everything) on (?P<host>[\w.-]+)$", self.update_host),
            (r"^(update|upgrade) (the )?(?P<names>[\w.,\s-]+?) containers? on (?P<host>[\w.-]+)$", self.update_named_containers),
            (r"^(update|upgrade) (?P<host>[\w.-]+)$", self.update_host),
            (r"^restart (the )?(container )?(?P<c>[\w.-]+)( container)? on (?P<host>[\w.-]+)$", self.restart_container_cmd),
            (r"^(clean ?up|prune) docker on (?P<host>[\w.-]+)$", self.docker_cleanup_cmd),
            (r"^reboot (?P<host>[\w.-]+)$", self.reboot_cmd),
            (r"^(create|make|build|spin up|new) (a )?(new )?vm (called |named )?(?P<name>[\w-]+)(?P<spec> .+)?$", self.create_vm),
            (r"^(media status|media)$", self.media_status),
            (r"^(status|how('s| is) (everything|the lab|it going))$", self.lab_status),
            (r"^(downloads?|queue|what'?s downloading)$", self.downloads),
            (r"^(remove|delete|cancel) (download )?(?P<n>\d+)$", self.remove_download),
            (r"^(torrents|usenet|clients|download clients|speeds?|download speeds?|how fast( is it)?)$", self.clients),
            (r"^(?P<act>pause|resume|stop|start|unpause) (?P<n>\d+)$", self.pause_item),
            (r"^(?P<act>pause|resume|stop|start|unpause) (all )?(the )?(?P<what>downloads|downloading|everything|torrents|usenet|qbittorrent|qbit|deluge|sabnzbd|sab)$", self.pause_all),
            (r"^(slow (mode|down)( downloads)?( (?P<state>on|off))?|(?P<fast>full speed|speed up|unthrottle))$", self.slow_mode),
            (r"^(playing|now playing|streams?|who'?s (watching|streaming)( plex)?)$", self.playing),
            (r"^(recent|recently added|what'?s new)$", self.recent),
            (r"^(upcoming|calendar|what'?s coming( up)?)( (?P<days>\d+) days?)?$", self.upcoming),
            (rf"^(search|hunt) (for )?missing (?P<kind>{KIND_WORDS})$", self.search_missing),
            (rf"^(search|find|look ?up) (for )?(the |a )?(?P<kind>{KIND_WORDS}) (?P<q>.+)$", self.search),
            (rf"^(add|download|get|grab) (the |a )?(?P<kind>{KIND_WORDS}) (?P<q>.+)$", self.add),
            (r"^(requests|pending( requests)?|overseerr)$", self.requests),
            (r"^(?P<act>approve|decline|deny|reject) (request )?(?P<n>\d+|all)$", self.decide),
            (r"^(refresh|scan|update) plex( (library )?(?P<lib>.+))?$", self.refresh_plex),
            (r"^(indexers|check indexers|prowlarr)$", self.indexers),
            (r"^test indexers$", self.test_indexers),
        ]
        self.rules = [(re.compile(p, re.IGNORECASE), f) for p, f in self.rules]
        # With the AI on, these still answer instantly; everything else goes to the AI, which uses the
        # same handlers as tools but can work out multi-step jobs and recover when a step fails.
        self.direct = {self.help, self.new_chat, self.set_mode}

    def handle(self, text: str) -> str:
        # Keep the original case: shell commands and paths are case-sensitive.
        t = re.sub(r"^((hey|ok) )?jarvis[,:]?\s*|^please\s+|\s*please$|\?+$", "", text.strip(), flags=re.IGNORECASE).strip()
        rule = next(((fn, m) for pattern, fn in self.rules if (m := pattern.match(t))), None)
        if rule and (not self.brain or rule[0] in self.direct):
            return self._call(*rule)
        if self.brain:
            reply = self.brain.ask(text, self.say)
            if rule and getattr(self.brain, "unreachable", False):
                return f"({reply} So I used my built-in command instead.)\n" + self._call(*rule)
            return reply
        return "Sorry, I didn't understand that. Type 'help' to see what I can do. (Set up the AI brain in Setup to talk in plain English.)"

    def _call(self, fn, m) -> str:
        try:
            return fn(_Match(m))
        except requests.HTTPError as e:
            return f"The service answered with an error: HTTP {e.response.status_code} on {e.request.url.split('?')[0]}"
        except requests.RequestException as e:
            return f"I couldn't reach that service: {type(e).__name__}"
        except _NotSetUp as e:
            return str(e)
        except Exception as e:  # never let one bad answer kill the chat
            return f"Something went wrong: {type(e).__name__}: {e}"

    # ------------------------------------------------------------ permission modes

    @property
    def mode(self) -> str:
        return modes.get(self.cfg)

    def allow(self, question: str, level: str = "change", doing: str | None = None) -> bool:
        """The one gate every action passes through.

        level: 'read'        only looks (run_command with df, docker ps...): asks only in Ask mode
               'safe'        harmless change (pause, refresh Plex): never asks, blocked in Read-only
               'change'      everyday change: asks in Ask mode, just happens in Auto
               'destructive' deletes, reboots, stops things: always asks, whatever the mode"""
        from .audit import audit
        mode = self.mode
        if mode == "read-only" and level != "read":
            what = doing or question.splitlines()[0].split("?")[0]
            audit(f"BLOCKED (read-only mode): {what}")
            raise Blocked(f"Read-only mode is on, so this was blocked: {what}. Say 'auto mode' or 'ask mode' to let me make changes.")
        if level == "destructive":
            return self._asked(self.confirm("[destructive] " + question))
        if mode == "ask" and level in ("change", "read"):
            return self._asked(self.confirm(question))
        if level == "change":
            what = doing or question.splitlines()[0].split("?")[0]
            self.say(f"{what} (auto mode: not destructive, so I didn't ask)")
            audit(f"AUTO-approved: {what}")
        return True

    def _asked(self, yes: bool) -> bool:
        if not yes:
            self.declined = True  # the AI is told plainly, so it doesn't misread "Okay, not rebooting."
        return yes

    def set_mode(self, m) -> str:
        if not m["mode"]:
            return f"Mode: {modes.MODES[self.mode]}\nSwitch with 'ask mode', 'auto mode' or 'read-only mode'. Destructive actions always ask, in every mode."
        from .audit import audit
        new = modes.set_mode(m["mode"])
        audit(f"mode set to {new}")
        return modes.MODES[new]

    def help(self, m) -> str:
        return HELP + f"\n\nMode: {modes.MODES[self.mode]}"

    # ------------------------------------------------------------ helpers

    def _arr(self, kind_word: str):
        kind = KINDS[kind_word]
        arr = self.stack.for_kind(kind)
        if not arr:
            app = {"movie": "Radarr", "show": "Sonarr", "artist": "Lidarr"}[kind]
            raise _NotSetUp(f"{app} isn't set up in config.yaml, so I can't handle {kind}s yet.")
        return arr

    def _need(self, attr: str, app: str):
        client = getattr(self.stack, attr)
        if not client:
            raise _NotSetUp(f"{app} isn't set up in config.yaml.")
        return client

    def new_chat(self, m) -> str:
        if self.brain:
            self.brain.reset()
        return "Okay, fresh start."

    # ------------------------------------------------------------ status

    def lab_status(self, m) -> str:
        from .collectors import build_collectors
        from .monitor import collect_all, spoken_summary
        reports = collect_all(build_collectors(self.cfg, self.stack))
        return spoken_summary(reports)

    def media_status(self, m) -> str:
        from .collectors.media import MediaCollector
        from .monitor import spoken_summary
        if not self.stack:
            raise _NotSetUp("No media services are set up in config.yaml.")
        reports = MediaCollector(self.stack, self.cfg["thresholds"]).collect()
        out = []
        for r in reports:
            out.append(f"{r.name}: " + ("DOWN" if not r.reachable else ("needs attention" if r.problems else "OK")))
            out += [f"  {l}" for l in r.lines[1:]] + [f"  ! {p.message}" for p in r.problems]
        return "\n".join(out + ["", spoken_summary(reports)])

    # ------------------------------------------------------------ downloads

    def downloads(self, m) -> str:
        self.last_queue = [(a, i) for a in self.stack.arrs.values() for i in a.queue()]
        if not self.stack.arrs:
            raise _NotSetUp("Sonarr/Radarr/Lidarr aren't set up in config.yaml.")
        if not self.last_queue:
            return "Nothing is downloading right now."
        lines = []
        for n, (arr, i) in enumerate(self.last_queue, 1):
            size, left = i.get("size") or 0, i.get("sizeleft") or 0
            done = int((1 - left / size) * 100) if size else 0
            eta = f", {i['timeleft']} left" if i.get("timeleft") else ""
            flag = "  (STUCK)" if str(i.get("trackedDownloadStatus", "")).lower() in ("warning", "error") else ""
            lines.append(f"{n}. [{arr.name}] {i.get('title', '?')}: {i.get('status', '?')} {done}%{eta}{flag}")
        return "\n".join(lines)

    def remove_download(self, m) -> str:
        n = int(m["n"])
        if not self.last_queue or not 1 <= n <= len(self.last_queue):
            return "Run 'downloads' first so I know which one you mean."
        arr, item = self.last_queue[n - 1]
        title = item.get("title", "?")
        if not self.allow(f"Remove '{title}' from {arr.name} and the download client (its files are deleted), and blocklist that release so it grabs a different one?", "destructive"):
            return "Okay, left it alone."
        arr.remove_from_queue(item["id"], blocklist=True)
        self.last_queue.pop(n - 1)
        return f"Removed '{title}'. {arr.name} will look for another release."

    # ------------------------------------------------------------ download clients

    def _downloaders(self, what: str | None = None) -> list:
        clients = self.stack.downloaders
        if not clients:
            raise _NotSetUp("No download clients are set up in config.yaml.")
        what = {"qbit": "qbittorrent", "sab": "sabnzbd"}.get(what, what)
        if what in ("qbittorrent", "deluge", "sabnzbd"):
            clients = [c for c in clients if type(c).__name__.lower().startswith(what)]
        elif what == "torrents":
            clients = [c for c in clients if c.kind == "torrent"]
        elif what == "usenet":
            clients = [c for c in clients if c.kind == "usenet"]
        if not clients:
            raise _NotSetUp(f"{what} isn't set up in config.yaml.")
        return clients

    def clients(self, m) -> str:
        what = {"torrents": "torrents", "usenet": "usenet"}.get(m.group(1))
        self.last_items, out = [], []
        for c in self._downloaders(what):
            st = c.stats()
            items = c.items()
            out.append(f"{c.name}: down {fmt_speed(st['down'])}" + (f", up {fmt_speed(st['up'])}" if c.kind == "torrent" else ""))
            out += [f"  ({n})" for n in st["notes"]]
            for i in sorted(items, key=lambda i: (i["state"] != "downloading", -i["progress"])):
                self.last_items.append((c, i))
                eta = f", {i['eta']} left" if i["eta"] else ""
                err = f"  ERROR: {i['error']}" if i["error"] else ""
                out.append(f"  {len(self.last_items)}. {i['name'][:70]}: {i['state']} {i['progress']:.0f}%{eta}{err}")
            if not items:
                out.append("  nothing unfinished")
        return "\n".join(out)

    def pause_item(self, m) -> str:
        n = int(m["n"])
        if not self.last_items or not 1 <= n <= len(self.last_items):
            return "Run 'torrents' first so I know which one you mean."
        client, item = self.last_items[n - 1]
        pausing = m["act"] in ("pause", "stop")
        self.allow(f"{'Pause' if pausing else 'Resume'} {item['name'][:70]}", "safe")
        (client.pause if pausing else client.resume)([item["id"]])
        return f"{'Paused' if pausing else 'Resumed'} {item['name'][:70]} in {client.name}."

    def pause_all(self, m) -> str:
        what = m["what"] if m["what"] not in ("downloads", "downloading", "everything") else None
        clients = self._downloaders(what)
        pausing = m["act"] in ("pause", "stop")
        self.allow(f"{'Pause' if pausing else 'Resume'} " + ", ".join(c.name for c in clients), "safe")
        for c in clients:
            (c.pause if pausing else c.resume)()
        return f"{'Paused' if pausing else 'Resumed'} " + ", ".join(c.name for c in clients) + "."

    def slow_mode(self, m) -> str:
        on = not m["fast"] and m["state"] != "off"
        clients = self._downloaders()
        self.allow("Turn slow mode " + ("on" if on else "off"), "safe")
        for c in clients:
            c.set_slow(on)
        return ("Slow mode on for " if on else "Back to full speed on ") + ", ".join(c.name for c in clients) + "."

    # ------------------------------------------------------------ hosts and commands

    def _hosts(self) -> list[dict]:
        hosts = [h for h in self.cfg.get("linux_hosts") or [] if h.get("allow_commands", True)]
        return hosts + [h for h in self.cfg.get("unraid") or [] if h.get("allow_commands", False)]

    def hosts(self, m) -> str:
        hosts = self._hosts()
        if not hosts:
            return "No hosts are set up for commands. Add them under linux_hosts in config.yaml."
        return "I can run commands on:\n" + "\n".join(
            f"  {h['name']}: {h.get('user', 'root')}@{h['host']}" + (" (Windows, PowerShell)" if h.get("os") == "windows" else "") for h in hosts)

    def discover(self, m) -> str:
        from .discovery import Discovery
        try:
            guests = Discovery(self.cfg).scan()
        except ValueError as e:
            raise _NotSetUp(str(e))
        if not guests:
            return "Proxmox has no VMs or containers."
        lines = []
        for g in guests:
            where = f"{g['ip']} (from {g['ip_from']})" if g["ip"] else "no IP found"
            tag = f"in Jarvis as {g['in_jarvis']}" if g["in_jarvis"] else ("not supported" if not g["supported"] else "not added")
            lines.append(f"  {g['vmid']} {g['name']} [{g['type']}, {g['status']}] {where}: {tag}")
        new = sum(1 for g in guests if not g["in_jarvis"] and g["ip"] and g["supported"])
        tail = f"\n{new} can be added: Setup > Discovered on Proxmox." if new else ""
        return f"{len(guests)} guests on Proxmox:\n" + "\n".join(lines) + tail

    def run(self, m) -> str:
        from .audit import audit
        from .collectors.ssh_host import run_command
        host = next((h for h in self._hosts() if h["name"].lower() == m["host"]), None)
        if not host:
            names = ", ".join(h["name"] for h in self._hosts()) or "none"
            return f"I don't know a host called '{m['host']}' that allows commands. Known: {names}."
        cmd = m["cmd"].strip()
        level = modes.classify(cmd, host.get("os") == "windows")
        warning = "\n  WARNING: this looks destructive or disruptive." if level == "destructive" else ""
        shell = " in PowerShell" if host.get("os") == "windows" else ""
        if level == "read" and self.mode != "ask":
            self.say(f"Running on {host['name']}: {cmd}")
        if not self.allow(f"Run{shell} on {host['name']} ({host.get('user', 'root')}@{host['host']}):\n    {cmd}{warning}\nGo ahead?",
                          level, doing=f"Running on {host['name']}: {cmd}"):
            audit(f"run DECLINED on {host['name']}: {cmd}")
            return "Okay, not running it."
        timeout = host.get("command_timeout", 120)
        code, out = run_command(host, cmd, timeout)
        audit(f"run on {host['name']} (exit {code}): {cmd}")
        lines = out.rstrip().splitlines()
        shown = lines[-40:]
        head = f"{host['name']}: " + ("timed out" if code is None else f"exit {code}")
        if len(lines) > 40:
            head += f" (last 40 of {len(lines)} lines)"
        return head + ("\n" + "\n".join(shown) if shown else ", no output")

    # ------------------------------------------------------------ updating Jarvis

    def check_updates(self, m) -> str:
        from . import updater
        try:
            st = updater.check()
        except updater.UpdateError as e:
            return f"I couldn't check for updates: {e}"
        head = f"I'm on {st['commit']} from {st['date']} ({st['subject']})."
        if not st["new"]:
            return head + " That's the latest."
        lines = "\n".join(f"  {c['commit']} {c['date']} {c['subject']}" for c in st["new"][:15])
        return f"{head}\n{len(st['new'])} update(s) available:\n{lines}\nSay 'update jarvis' to install."

    def update_self(self, m) -> str:
        from . import updater
        try:
            st = updater.check()
            if not st["new"]:
                return f"Already on the latest version ({st['commit']})."
            if not self.allow(f"Update Jarvis from {st['commit']} with {len(st['new'])} new change(s)? I'll restart afterwards.",
                              doing=f"Updating Jarvis with {len(st['new'])} new change(s), then restarting"):
                return "Okay, not updating."
            result = updater.apply()
        except updater.UpdateError as e:
            return f"Update failed: {e}"
        if updater.restart_soon(3):
            return result["message"] + " Restarting now; I'll be back in a few seconds."
        return result["message"] + " Restart Jarvis to use the new version."

    # ------------------------------------------------------------ housekeeping

    def _host_named(self, name: str) -> dict:
        host = next((h for h in self._hosts() if h["name"].lower() == str(name).lower()), None)
        if not host:
            names = ", ".join(h["name"] for h in self._hosts()) or "none"
            raise _NotSetUp(f"I don't know a host called '{name}' that allows commands. Known: {names}.")
        return host

    def _plan(self, host: dict, packages: bool = True, containers: bool = True, names: list | None = None) -> dict:
        from . import housekeeping as hk
        plan = {"host": host, "packages": None, "containers": [], "notes": [], "errors": []}
        if packages:
            self.say(f"Checking {host['name']} for OS updates...")
            try:
                plan["packages"] = hk.check_packages(host)
                if plan["packages"]["note"]:
                    plan["notes"].append(plan["packages"]["note"])
                plan["notes"] += plan["packages"]["warnings"]
            except hk.HousekeepingError as e:
                plan["errors"].append(str(e))
        if containers and host.get("os") != "windows":
            self.say(f"Checking {host['name']}'s containers for new images (this downloads them; nothing restarts yet)...")
            try:
                plan["containers"] = hk.check_containers(host, names)
            except hk.HousekeepingError as e:
                if names:
                    raise
                if "isn't running" not in str(e):
                    plan["errors"].append(str(e))
        return plan

    @staticmethod
    def _plan_work(plan: dict) -> tuple[list[str], list, list]:
        pk = plan["packages"]
        pkgs = pk["packages"] if pk and pk["pm"] in ("apt", "dnf", "yum", "apk", "pacman", "zypper") else []
        ups = [c for c in plan["containers"] if c.get("status") == "update"]
        parts = []
        if pkgs:
            shown = ", ".join(pkgs[:8]) + (f" and {len(pkgs) - 8} more" if len(pkgs) > 8 else "")
            parts.append(f"{len(pkgs)} OS update(s) ({shown})")
        if ups:
            parts.append(f"new images for {len(ups)} container(s) ({', '.join(c['name'] for c in ups)})")
        return parts, pkgs, ups

    def _apply(self, plan: dict, pkgs: list, ups: list) -> tuple[list[str], bool]:
        from . import housekeeping as hk
        from .audit import audit
        host, lines = plan["host"], []
        name = host["name"]
        reboot = bool(plan["packages"] and plan["packages"]["reboot"])
        if pkgs:
            self.say(f"Installing {len(pkgs)} OS update(s) on {name}. This can take several minutes...")
            try:
                r = hk.apply_packages(host, plan["packages"]["pm"], plan["packages"].get("proxmox"))
                audit(f"housekeeping: installed {len(pkgs)} OS update(s) on {name}")
                lines.append(f"{name}: installed {len(pkgs)} OS update(s).")
                reboot = reboot or r["reboot"]
            except hk.HousekeepingError as e:
                audit(f"housekeeping: OS update FAILED on {name}: {e}")
                lines.append(f"{name}: {e}")
        if ups:
            self.say(f"Recreating {len(ups)} container(s) on {name} with their new images...")
            try:
                for r in hk.apply_containers(host, ups):
                    audit(f"housekeeping: container {r['name']} on {name}: {r['result']}")
                    if r["result"] == "updated":
                        lines.append(f"{name}: updated {r['name']}.")
                    elif r["result"] == "manual":
                        lines.append(f"{name}: {r['name']}: {r['detail']}.")
                    else:
                        lines.append(f"{name}: updating {r['name']} failed: {r['detail']}")
            except hk.HousekeepingError as e:
                lines.append(f"{name}: {e}")
        return lines, reboot

    def _offer_reboot(self, hosts: list) -> str:
        from . import housekeeping as hk
        from .audit import audit
        if not hosts:
            return ""
        names = ", ".join(h["name"] for h in hosts)
        verb = "needs" if len(hosts) == 1 else "need"
        if not self.allow(f"{names} {verb} a reboot to finish updating. Reboot now?\n  WARNING: anything running there stops until it's back.", "destructive"):
            return f"\n{names} still {verb} a reboot. Say 'reboot <name>' when it suits you."
        done = []
        for h in hosts:
            try:
                hk.reboot(h)
                audit(f"housekeeping: rebooted {h['name']}")
                done.append(f"Rebooting {h['name']}.")
            except hk.HousekeepingError as e:
                done.append(str(e))
        return "\n" + " ".join(done)

    def _update(self, hosts: list, packages: bool, containers: bool, names: list | None = None) -> str:
        from . import housekeeping as hk
        plans, notes, work = [], [], []
        for h in hosts:
            try:
                plans.append(self._plan(h, packages, containers, names))
            except hk.HousekeepingError as e:
                return str(e)
        for p in plans:
            parts, pkgs, ups = self._plan_work(p)
            notes += [f"{p['host']['name']}: {n}" for n in p["notes"]]
            if parts:
                work.append((p, pkgs, ups, f"{p['host']['name']}: " + " and ".join(parts)))
        tail = "".join(f"\n  note: {n}" for n in notes)
        problems = [f"Couldn't check {p['host']['name']}: {e}" for p in plans for e in p["errors"]]
        if not work:
            failed = {p["host"]["name"] for p in plans if p["errors"]}
            if len(failed) == len(plans):
                return "\n".join(problems) + tail
            ok = ("Everything else is up to date." if failed else
                  "Everything is up to date." if len(hosts) > 1 else f"{hosts[0]['name']} is up to date.")
            return "\n".join(problems + [ok]) + tail
        tail = "".join(f"\n  {p}" for p in problems) + tail
        summary = "\n".join("  " + w[3] for w in work)
        if not self.allow(f"Install these updates?\n{summary}", doing=f"Installing updates:\n{summary}"):
            return "Okay, nothing was changed."
        lines, reboot_hosts = [], []
        for p, pkgs, ups, _ in work:
            done, needs_reboot = self._apply(p, pkgs, ups)
            lines += done
            if needs_reboot:
                reboot_hosts.append(p["host"])
        return "\n".join(lines) + tail + self._offer_reboot(reboot_hosts)

    def host_updates_check(self, m) -> str:
        target = m["host"] or "all"
        hosts = self._hosts() if target.startswith("all") or target == "everything" else [self._host_named(target)]
        if not hosts:
            raise _NotSetUp("No hosts are set up for commands yet. Add them in Setup.")
        out = []
        for h in hosts:
            plan = self._plan(h)
            parts, _, _ = self._plan_work(plan)
            status = "; ".join(parts) if parts else ("couldn't check" if plan["errors"] else "up to date")
            if plan["packages"] and plan["packages"]["reboot"]:
                status += "; a reboot is pending"
            fails = [c["name"] for c in plan["containers"] if c.get("status") == "pullfail"]
            if fails:
                status += f"; couldn't check {', '.join(fails)} (local or private images)"
            out.append(f"{h['name']}: {status}" + "".join(f"\n  problem: {e}" for e in plan["errors"])
                       + "".join(f"\n  note: {n}" for n in plan["notes"]))
        installable = any(("update(s)" in o.split("\n")[0] or "new images" in o.split("\n")[0]) for o in out)
        return "\n".join(out) + ("\nSay 'update <name>' (or 'update everything') to install." if installable else "")

    def update_host(self, m) -> str:
        host = self._host_named(m["host"])
        what = (m["what"] or "everything").replace("all ", "")
        return self._update([host], what in ("everything", "packages", "the os", "os", "system"), what in ("everything", "containers"))

    def update_named_containers(self, m) -> str:
        host = self._host_named(m["host"])
        names = [n for n in re.split(r"[,\s]+", m["names"]) if n and n != "and"]
        return self._update([host], False, True, names)

    def update_everything(self, m) -> str:
        hosts = self._hosts()
        if not hosts:
            raise _NotSetUp("No hosts are set up for commands yet. Add them in Setup.")
        return self._update(hosts, True, True)

    def update_selected(self, host: str, packages: bool, containers: bool, names: list | None = None) -> str:
        """For the AI: host 'all' means every machine."""
        if names:
            return self._update([self._host_named(host)], False, True, names)
        hosts = self._hosts() if host.lower() in ("all", "everything") else [self._host_named(host)]
        return self._update(hosts, packages, containers)

    def restart_container_cmd(self, m) -> str:
        from . import housekeeping as hk
        from .audit import audit
        host = self._host_named(m["host"])
        if not self.allow(f"Restart container {m['c']} on {host['name']}?"):
            return "Okay, left it alone."
        try:
            name = hk.restart_container(host, m["c"])
        except hk.HousekeepingError as e:
            return str(e)
        audit(f"housekeeping: restarted container {name} on {host['name']}")
        return f"Restarted {name} on {host['name']}."

    def docker_cleanup_cmd(self, m) -> str:
        from . import housekeeping as hk
        from .audit import audit
        host = self._host_named(m["host"])
        if not self.allow(f"Remove unused Docker images and build cache on {host['name']}? Containers and volumes "
                          "aren't touched; an image is downloaded again if something needs it later.", "destructive"):
            return "Okay, nothing was removed."
        try:
            result = hk.docker_cleanup(host)
        except hk.HousekeepingError as e:
            return str(e)
        audit(f"housekeeping: docker cleanup on {host['name']}: {result}")
        return result

    def reboot_cmd(self, m) -> str:
        from . import housekeeping as hk
        from .audit import audit
        host = self._host_named(m["host"])
        if not self.allow(f"Reboot {host['name']} ({host['host']}) now?\n  WARNING: anything running there stops until it's back.", "destructive"):
            return "Okay, not rebooting."
        try:
            hk.reboot(host)
        except hk.HousekeepingError as e:
            return str(e)
        audit(f"housekeeping: rebooted {host['name']}")
        return f"Rebooting {host['name']}. It should be back in a minute or two."

    # ------------------------------------------------------------ VMs

    def _provisioner(self):
        from .provision import ProvisionError, Provisioner
        try:
            return Provisioner(self.cfg)
        except ProvisionError as e:
            raise _NotSetUp(str(e))

    def check_vm_setup(self, m) -> str:
        return "\n".join(self._provisioner().check())

    def create_vm(self, m) -> str:
        from .provision import ProvisionError
        spec = m["spec"] or ""
        cores = re.search(r"(\d+)\s*(v?cpus?|cores?)", spec)
        mem = re.search(r"(\d+)\s*(gb|g|mb|m)?\s*(of\s+)?(ram|memory)", spec)
        disk = re.search(r"(\d+)\s*(tb|t|gb|g)?\s*(of\s+)?(disk|storage|hdd|ssd)", spec)
        memory_mb = None
        if mem:
            memory_mb = int(mem[1]) if (mem[2] or "g").startswith("m") else int(mem[1]) * 1024
        disk_gb = None
        if disk:
            disk_gb = int(disk[1]) * (1024 if (disk[2] or "g").startswith("t") else 1)
        prov = self._provisioner()
        try:
            plan = prov.plan(m["name"], int(cores[1]) if cores else None, memory_mb, disk_gb)
            if not self.allow(prov.describe(plan), doing=f"Creating VM {m['name']}"):
                return "Okay, nothing was created."
            result = prov.execute(plan, self.say)
        except ProvisionError as e:
            return f"Couldn't create the VM: {e}"
        tail = (f"It's answering on SSH. Try: run uptime on {result['name']}" if result["ssh_up"]
                else "It hasn't answered on SSH yet. Cloud-init may still be running; give it a minute, then try: run uptime on " + result["name"])
        return (f"{result['name']} is up: VM {result['vmid']}, {result['ip']} (reserved on pfSense for {result['mac']}). "
                f"I'll monitor it from now on. {tail}")

    # ------------------------------------------------------------ Plex

    def playing(self, m) -> str:
        plex = self._need("plex", "Plex")
        sessions = plex.sessions()
        if not sessions:
            return "Nobody is watching anything right now."
        return "\n".join(plex.describe_session(s) for s in sessions)

    def recent(self, m) -> str:
        plex = self._need("plex", "Plex")
        items = plex.recently_added(10)
        return "Recently added to Plex:\n" + "\n".join(f"  {PlexClient.label(i)}" for i in items) if items else "Nothing new lately."

    def refresh_plex(self, m) -> str:
        plex = self._need("plex", "Plex")
        libs = plex.libraries()
        if m["lib"]:
            libs = [l for l in libs if m["lib"].strip() in l.get("title", "").lower()]
            if not libs:
                return f"I couldn't find a Plex library called '{m['lib']}'."
        self.allow("Scan Plex libraries", "safe")
        for lib in libs:
            plex.refresh(lib["key"])
        return "Scanning " + ", ".join(l["title"] for l in libs) + "."

    # ------------------------------------------------------------ *arr

    def upcoming(self, m) -> str:
        days = int(m["days"] or 7)
        items = sorted(i for a in self.stack.arrs.values() for i in a.upcoming(days))
        if not items:
            return f"Nothing scheduled in the next {days} days."
        return f"Coming up in the next {days} days:\n" + "\n".join(f"  {d}  {t}" for d, t in items)

    def search(self, m) -> str:
        arr = self._arr(m["kind"])
        results = arr.lookup(m["q"])[:8]
        if not results:
            return f"{arr.name} found nothing for '{m['q']}'."
        return "\n".join(f"{n}. {arr.label(r)}" + ("  (already in library)" if arr.in_library(r) else "")
                         for n, r in enumerate(results, 1))

    def add(self, m) -> str:
        arr = self._arr(m["kind"])
        results = arr.lookup(m["q"])
        if m["year"]:  # the AI can name the exact one, e.g. Dune (2021)
            results = [r for r in results if str(r.get("year")) == str(m["year"])] or results
        results = results[:5]
        if not results:
            return f"{arr.name} found nothing for '{m['q']}'."
        pick = 0 if len(results) == 1 else self._obvious_pick(results, m["q"], m["year"], arr)
        if pick is None:
            pick = self.choose([arr.label(r) + ("  (already in library)" if arr.in_library(r) else "") for r in results])
        if pick is None:
            return "Okay, cancelled."
        item = results[pick]
        if arr.in_library(item):
            return f"{arr.label(item)} is already in {arr.name}."
        if not self.allow(f"Add {arr.label(item)} to {arr.name} and start searching for it?"):
            return "Okay, cancelled."
        arr.add(item)
        return f"Added {arr.label(item)}. {arr.name} is searching for it now."

    def _obvious_pick(self, results: list, query: str, year, arr) -> int | None:
        """Outside Ask mode, don't make the user pick when one result is clearly the one they meant."""
        if self.mode == "ask":
            return None
        q = re.sub(r"[^a-z0-9]+", " ", str(query).lower()).strip()
        exact = [i for i, r in enumerate(results)
                 if re.sub(r"[^a-z0-9]+", " ", str(r.get("title") or r.get("artistName") or "").lower()).strip() == q
                 and (not year or str(r.get("year")) == str(year))]
        if len(exact) == 1:
            self.say(f"Picked {arr.label(results[exact[0]])}, the exact match.")
            return exact[0]
        return None

    def search_missing(self, m) -> str:
        arr = self._arr(m["kind"])
        if not self.allow(f"Ask {arr.name} to search for everything missing? This can hit your indexers hard."):
            return "Okay, cancelled."
        arr.search_missing()
        return f"{arr.name} is searching for missing {KINDS[m['kind']]}s."

    # ------------------------------------------------------------ Overseerr

    def requests(self, m) -> str:
        o = self._need("overseerr", "Overseerr")
        self.last_requests = o.pending()
        if not self.last_requests:
            return "No requests waiting for approval."
        return "\n".join(f"{n}. {o.title_of(r)} [{r.get('type', '?')}], requested by "
                         f"{(r.get('requestedBy') or {}).get('displayName', '?')}"
                         for n, r in enumerate(self.last_requests, 1))

    def decide(self, m) -> str:
        o = self._need("overseerr", "Overseerr")
        if not self.last_requests:
            return "Run 'requests' first so I know which one you mean."
        approve = m["act"] == "approve"
        if m["n"] == "all":
            chosen = list(self.last_requests)
        else:
            n = int(m["n"])
            if not 1 <= n <= len(self.last_requests):
                return f"There's no request {n}. I listed {len(self.last_requests)}."
            chosen = [self.last_requests[n - 1]]
        names = ", ".join(o.title_of(r) for r in chosen)
        if not self.allow(f"{'Approve' if approve else 'Decline'} {names}?"):
            return "Okay, cancelled."
        for r in chosen:
            (o.approve if approve else o.decline)(r["id"])
            self.last_requests.remove(r)
        return f"{'Approved' if approve else 'Declined'}: {names}."

    # ------------------------------------------------------------ Prowlarr

    def indexers(self, m) -> str:
        p = self._need("prowlarr", "Prowlarr")
        idx = p.indexers()
        status = {s.get("indexerId"): s for s in p.indexer_status()}
        lines = []
        for i in sorted(idx, key=lambda i: i.get("name", "")):
            s = status.get(i["id"])
            state = "disabled" if not i.get("enable") else (f"failing until {s['disabledTill'][:16].replace('T', ' ')}" if s and s.get("disabledTill") else "ok")
            lines.append(f"  {i.get('name')}: {state}")
        return f"{len(idx)} indexers:\n" + "\n".join(lines)

    def test_indexers(self, m) -> str:
        p = self._need("prowlarr", "Prowlarr")
        self.allow("Test every Prowlarr indexer", "safe")
        names = {i["id"]: i.get("name") for i in p.indexers()}
        try:
            results = p.test_all()
        except requests.HTTPError as e:  # Prowlarr answers 400 when any indexer fails, with the results in the body
            results = e.response.json() if e.response is not None and e.response.content else []
        bad = [names.get(r.get("id"), r.get("id")) for r in results if not r.get("isValid")]
        return f"All {len(results)} indexers passed." if not bad else f"{len(bad)} failed: {', '.join(map(str, bad))}"


class _NotSetUp(Exception):
    pass


class Blocked(_NotSetUp):
    """Read-only mode stopped an action."""


class _Match:
    """Captured words lowercased for easy comparison, except 'cmd', which must stay exactly as typed."""

    def __init__(self, m: re.Match):
        self.m = m

    def __getitem__(self, key):
        try:
            v = self.m[key]
        except (IndexError, KeyError):  # a group this rule doesn't have
            return None
        return v if key == "cmd" or v is None else str(v).lower()

    def group(self, i):
        v = self.m.group(i)
        return v.lower() if v else v


def chat(cfg: dict) -> None:
    jarvis = Assistant(cfg)
    print(f"Jarvis here. Type 'help' for ideas, 'exit' to quit.\nMode: {modes.MODES[jarvis.mode]}")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in ("exit", "quit", "bye"):
            break
        if text:
            print(jarvis.handle(text))
    print("Jarvis signing off.")
