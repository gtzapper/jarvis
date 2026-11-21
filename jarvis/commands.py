"""Text commands. Each handler is a plain function, so the future AI brain can call the same ones
as tools when you speak in free-form English."""

import re
from typing import Callable

import requests

from .media.clients import MediaStack, PlexClient
from .media.downloaders import fmt_speed

RISKY = re.compile(r"\brm\s+-\w*[rf]|\bmkfs|\bdd\s+if=|\b(shutdown|reboot|poweroff|halt)\b|>\s*/dev/(sd|nvme|vd)|\bwipefs|\bfdisk|\bparted"
                   r"|:\(\)\s*\{|\bchmod\s+-R|\bchown\s+-R|\buserdel|\bsystemctl\s+(stop|disable|mask)|\bdocker\s+(rm|system\s+prune|volume\s+rm)"
                   r"|\bapt(-get)?\s+(remove|purge|autoremove)|\biptables\s+-F|\bkill(all)?\b", re.IGNORECASE)

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
  run uptime on web1             run a shell command on a host (always asks first)
  create vm web1 [with 4 cores 8gb ram 64gb disk]   new VM from your template + static IP on pfSense
  check vm setup                 test the VM-creation permissions without changing anything
  new chat                       start a fresh AI conversation
  help                           this list
With the AI brain set up, you can also just say what you want in plain English."""


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
            (r"^(help|\?|what can you do)$", lambda m: HELP),
            (r"^(new chat|new conversation|reset|forget (it|that|everything))$", self.new_chat),
            (r"^(hosts|list hosts|machines|servers)$", self.hosts),
            (r"^(run|exec|execute) (?P<cmd>.+?) on (?P<host>[\w.-]+)$", self.run),
            (r"^on (?P<host>[\w.-]+)[,:]? (run |exec |execute )?(?P<cmd>.+)$", self.run),
            (r"^check (vm|provisioning) setup$", self.check_vm_setup),
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

    def handle(self, text: str) -> str:
        # Keep the original case: shell commands and paths are case-sensitive.
        t = re.sub(r"^((hey|ok) )?jarvis[,:]?\s*|^please\s+|\s*please$|\?+$", "", text.strip(), flags=re.IGNORECASE).strip()
        for pattern, fn in self.rules:
            m = pattern.match(t)
            if m:
                m = _Match(m)
                try:
                    return fn(m)
                except requests.HTTPError as e:
                    return f"The service answered with an error: HTTP {e.response.status_code} on {e.request.url.split('?')[0]}"
                except requests.RequestException as e:
                    return f"I couldn't reach that service: {type(e).__name__}"
                except _NotSetUp as e:
                    return str(e)
                except Exception as e:  # never let one bad answer kill the chat
                    return f"Something went wrong: {type(e).__name__}: {e}"
        if self.brain:  # anything that isn't an exact command goes to the AI
            return self.brain.ask(text, self.say)
        return "Sorry, I didn't understand that. Type 'help' to see what I can do. (Set up the AI brain in Setup to talk in plain English.)"

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
        if not self.confirm(f"Remove '{title}' from {arr.name} and the download client, and blocklist that release so it grabs a different one?"):
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
        (client.pause if pausing else client.resume)([item["id"]])
        return f"{'Paused' if pausing else 'Resumed'} {item['name'][:70]} in {client.name}."

    def pause_all(self, m) -> str:
        what = m["what"] if m["what"] not in ("downloads", "downloading", "everything") else None
        clients = self._downloaders(what)
        pausing = m["act"] in ("pause", "stop")
        for c in clients:
            (c.pause if pausing else c.resume)()
        return f"{'Paused' if pausing else 'Resumed'} " + ", ".join(c.name for c in clients) + "."

    def slow_mode(self, m) -> str:
        on = not m["fast"] and m["state"] != "off"
        clients = self._downloaders()
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
        return "I can run commands on:\n" + "\n".join(f"  {h['name']}: {h.get('user', 'root')}@{h['host']}" for h in hosts)

    def run(self, m) -> str:
        from .audit import audit
        from .collectors.ssh_host import run_command
        host = next((h for h in self._hosts() if h["name"].lower() == m["host"]), None)
        if not host:
            names = ", ".join(h["name"] for h in self._hosts()) or "none"
            return f"I don't know a host called '{m['host']}' that allows commands. Known: {names}."
        cmd = m["cmd"].strip()
        warning = "\n  WARNING: this looks destructive or disruptive." if RISKY.search(cmd) else ""
        if not self.confirm(f"Run on {host['name']} ({host.get('user', 'root')}@{host['host']}):\n    {cmd}{warning}\nGo ahead?"):
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
            if not self.confirm(prov.describe(plan)):
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
        pick = 0 if len(results) == 1 else self.choose([arr.label(r) + ("  (already in library)" if arr.in_library(r) else "") for r in results])
        if pick is None:
            return "Okay, cancelled."
        item = results[pick]
        if arr.in_library(item):
            return f"{arr.label(item)} is already in {arr.name}."
        if not self.confirm(f"Add {arr.label(item)} to {arr.name} and start searching for it?"):
            return "Okay, cancelled."
        arr.add(item)
        return f"Added {arr.label(item)}. {arr.name} is searching for it now."

    def search_missing(self, m) -> str:
        arr = self._arr(m["kind"])
        if not self.confirm(f"Ask {arr.name} to search for everything missing? This can hit your indexers hard."):
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
        if not self.confirm(f"{'Approve' if approve else 'Decline'} {names}?"):
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
        names = {i["id"]: i.get("name") for i in p.indexers()}
        try:
            results = p.test_all()
        except requests.HTTPError as e:  # Prowlarr answers 400 when any indexer fails, with the results in the body
            results = e.response.json() if e.response is not None and e.response.content else []
        bad = [names.get(r.get("id"), r.get("id")) for r in results if not r.get("isValid")]
        return f"All {len(results)} indexers passed." if not bad else f"{len(bad)} failed: {', '.join(map(str, bad))}"


class _NotSetUp(Exception):
    pass


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
    print("Jarvis here. Type 'help' for ideas, 'exit' to quit.")
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
