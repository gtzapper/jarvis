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

# What a family member's chat can do (everything else is the owner's)
FAMILY_COMMANDS = {"help", "new_chat", "remind", "list_reminders", "cancel_reminder", "add_note", "list_notes", "find_notes",
                   "list_add", "show_list", "list_check_cmd", "list_remove", "list_clear", "weather", "watch_cmd",
                   "request_media_cmd", "recent", "upcoming", "playing", "web_search"}

FAMILY_HELP = """Things I can help with:
  remind me in 20 minutes to check the oven / reminders / cancel reminder 3
  note: the wifi password is on the fridge / notes / find notes wifi
  add milk and eggs to the shopping list / shopping list / got milk
  weather / weather tomorrow / will it rain
  what should I watch / recent / upcoming releases / who's watching
  request the movie The Wild Robot / request the show Bluey   (the owner approves it; you hear when it's ready)
  search the web for ...   (if the owner has set up web search)
Your reminders and notes are your own. Lists are shared with the household."""

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
  upcoming releases [14 days]    episodes, movies and albums coming soon
  remind me in 20 minutes to check the oven / remind me to pay rent on the 1st every month
  reminders / cancel reminder 3  your reminders
  note: the NAS fan is a Noctua  save a note (notes, find notes nas)
  agenda / bills / what's coming up   events, bills and reminders ahead
  add bill rent 1200 on the 1st monthly / add event dentist on 5 December at 14:30
  mark rent paid                 moves a repeating bill to its next due date
  daily report                   the morning briefing, any time
  check my email / important emails   what your local AI found in new mail (add bill from email 12)
  packages / subscriptions           deliveries and what you pay for, found in your email
  reply to priya: yes, 8pm works     writes an email; you see it and approve it first (sending is off until Setup)
  add milk and eggs to the shopping list / shopping list / got milk / clear done items
  remember that my car needs an oil change every 10,000 km / what do you remember / forget oil
  add habit take meds at 21:00 / done meds / habits
  weather / weather tomorrow / will it rain
  security digest / open ports / internet speed / public ip / name device 10.0.20.44 as Living room TV
  ask my documents when does my car insurance renew / my documents
  where am I / where was I yesterday at 3pm / how far did I travel this week / location today
  remind me to take the bins out when I get home / call this place Gym
  search the web for best 2.5GbE switch   (with the AI on, Jarvis reads the pages and sums up with sources)
  search movie dune              look something up (also: show, artist)
  add movie dune                 add it and start searching (also: show, artist)
  search missing movies          ask Radarr/Sonarr/Lidarr to hunt for missing items
  requests                       requests waiting for your OK (Jarvis and Overseerr), numbered
  what should I watch            carry on watching, plus unwatched picks that suit your taste
  clean up / free up space       big things watched long ago or never watched (delete movie X asks first; keep X)
  request the movie Dune for Maya   queue a request for your OK; you hear when it's ready
  approve 2 / decline 2 / approve all
  refresh plex [library name]    scan libraries for new files
  indexers / test indexers       Prowlarr indexer health
  hosts                          machines I can run commands on
  scan proxmox                   find every VM/LXC, its IP, and whether Jarvis can log in
  find media apps                find Plex, the *arrs and download clients in Docker, VMs and LXCs
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


def ask_yes_with_code(question: str) -> tuple[bool, str]:
    if not ask_yes(question):
        return False, ""
    return True, input("Code from your authenticator app: ").strip()


def ask_choice(options: list[str]) -> int | None:
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    pick = input(f"Which one? (1-{len(options)}, Enter to cancel) ").strip()
    return int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(options) else None


class Assistant:
    def __init__(self, cfg: dict, stack: MediaStack | None = None,
                 confirm: Callable[[str], bool] = ask_yes,
                 choose: Callable[[list[str]], int | None] = ask_choice,
                 say: Callable[[str], None] = print, user: dict | None = None):
        self.cfg = cfg
        self.user = user  # a family member's account limits what this assistant can do; None = the owner
        self.family = bool(user and user.get("role") == "family")
        self.uid = user["id"] if self.family else None  # whose reminders and notes
        self.stack = stack if stack is not None else MediaStack(cfg)
        self.confirm = confirm
        self.confirm_code = ask_yes_with_code  # destructive actions when two-factor approval is on
        self.choose = choose
        self.say = say  # progress updates during long jobs
        self.brain = None
        self.report_source = None  # the web server plugs in a report that includes live lab status
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
            (r"^(find|discover|scan for|look for) (my |the )?(media( apps| services| stack)?|arr apps|\*arrs?)$", self.discover_media),
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
            (r"^(upcoming (releases|episodes|movies)|releases|what'?s coming out)( (?P<days>\d+) days?)?$", self.upcoming),
            (r"^remind me (to )?(?P<what>.+?) when I (?P<ev>get (to|home)|arrive (at|home)|reach|leave|get back( to)?)\s*(?P<place>.*)$", self.remind_at_place),
            (r"^remind me (?P<rest>.+)$", self.remind),
            (r"^(reminders|my reminders|list reminders|show reminders)$", self.list_reminders),
            (r"^(cancel|delete|remove) reminder (?P<n>\d+)$", self.cancel_reminder),
            (r"^(note|take a note|add a note|add note|make a note)( that)?[:,]? (?P<text>.+)$", self.add_note),
            (r"^(notes|my notes|show notes|list notes)$", self.list_notes),
            (r"^((find|search)( my)? notes?( for| about)?|what did i note about) (?P<q>.+)$", self.find_notes),
            (r"^(agenda|calendar|my calendar|upcoming|what'?s (coming up|on|happening)( this week| today)?|schedule|my schedule)( (?P<days>\d+) days?)?$", self.agenda),
            (r"^(bills|my bills|upcoming bills|bills due|what bills are due|which bills are due)$", self.bills),
            (r"^(add|new) bill (?!from (e-?mail|mail) )(?P<rest>.+)$", self.add_bill),
            (r"^(add|new) event (?P<rest>.+)$", self.add_event),
            (r"^(mark |i paid |paid )(the )?(?P<name>.+?)( bill)?( as)?( paid)?$", self.mark_paid),
            (r"^(check (my )?(e-?mails?|inbox|mail)|(my )?(e-?mails?|inbox|mail)( summary)?|any (new |important )?(e-?mails?|mail)|important (e-?mails?|mail))$", self.email_summary),
            (r"^add (the )?bill from (e-?mail|mail) (?P<n>\d+)$", self.email_bill),
            (r"^(((any|my) )?(packages?|parcels?|deliveries|orders)( coming)?|where'?s my (package|parcel|order|delivery)|what'?s (arriving|being delivered)( today)?)$", self.packages_cmd),
            (r"^(subscriptions|my subscriptions|subscription audit|audit (my )?subscriptions|what am i (subscribed to|paying for))$", self.subscriptions_cmd),
            (r"^(i )?(cancell?ed|unsubscribed from) (my )?(?P<name>.+?)( subscription)?$", self.cancelled_cmd),
            (r"^reply to (e-?mail |mail )?(?P<who>.+?)\s*(:|saying|that)\s*(?P<body>.+)$", self.reply_cmd),
            (r"^(e-?mail|send an e-?mail to|write to) (?P<who>\S+@\S+|[\w .'-]+?)\s*(:|saying|that|to say)\s*(?P<body>.+)$", self.email_cmd),
            (r"^(add|new|track) habit (?P<rest>.+)$", self.habit_add_cmd),
            (r"^(add|put) (?P<items>.+?) (to|on) (my |the )?(?P<name>[\w -]+?)( list)?$", self.list_add),
            (r"^(show |what'?s on |read )?(my |the )?(?P<name>shopping|grocery|groceries|todo|to-do|to do|[\w-]+) list$", self.show_list),
            (r"^(lists|my lists|all lists)$", self.show_list),
            (r"^(got|bought|done with|tick off|check off|cross off) (?P<item>.+?)( (from|on) (my |the )?(?P<name>[\w -]+?)( list)?)?$", self.list_check_cmd),
            (r"^(remove|delete) (?P<item>.+?) from (my |the )?(?P<name>[\w -]+?)( list)?$", self.list_remove),
            (r"^clear (the )?(done|ticked|bought|completed)( items)?( from (my |the )?(?P<name>[\w -]+?)( list)?)?$", self.list_clear),
            (r"^remember( that)?[:,]? (?P<fact>.+)$", self.remember_cmd),
            (r"^(what do you remember|what do you know about me|memories|my memories)$", self.list_memories),
            (r"^forget( about)?[:,]? (?P<what>.+)$", self.forget_cmd),
            (r"^(done|did|finished|i did|i took|took) (?P<name>.+)$", self.habit_done),
            (r"^(habits|my habits|habit streaks)$", self.habit_list),
            (r"^(weather|what'?s the weather( like)?|forecast|will it rain|is it going to rain)( (?P<when>today|tomorrow))?$", self.weather),
            (r"^(security digest|security report|security summary|weekly security)$", self.security_digest),
            (r"^(open ports|port forwards|what'?s open to the internet|exposed ports)$", self.open_ports),
            (r"^(internet speed|speed test|speedtest|how fast is (the|my) internet|run a speed test)$", self.internet_speed),
            (r"^(public ip|my ip|what'?s my (public )?ip|external ip)$", self.public_ip),
            (r"^(name|label|call) device (?P<who>\S+) (as )?(?P<label>.+)$", self.name_device),
            (r"^(ask|search|check) (my )?(documents|docs|files)( for| about)?[:,]? (?P<q>.+)$", self.ask_documents),
            (r"^(documents|my documents|docs|my docs|receipts|my receipts)$", self.list_documents),
            (r"^(search the web|web search|google|look up online|research)( for)?[:,]? (?P<q>.+)$", self.web_search),
            (r"^(where am i|where'?s my phone|my location)$", self.where_am_i),
            (r"^where was i (?P<when>.+)$", self.where_was_i_cmd),
            (r"^(how far|distance)( did i (travel|go|drive))?( (?P<span>today|yesterday|this week))?$", self.how_far),
            (r"^(location|places|my day)( (?P<span>today|yesterday))?$", self.how_far),
            (r"^(call|name|save) (this|here|where i am)( place)?( as)? (?P<name>.+)$", self.name_here),
            (r"^(daily report|morning report|my report|briefing|morning briefing|daily briefing|give me my (daily |morning )?(report|briefing))$", self.daily_report),
            (rf"^(search|hunt) (for )?missing (?P<kind>{KIND_WORDS})$", self.search_missing),
            (rf"^(search|find|look ?up) (for )?(the |a )?(?P<kind>{KIND_WORDS}) (?P<q>.+)$", self.search),
            (rf"^(add|download|get|grab) (the |a )?(?P<kind>{KIND_WORDS}) (?P<q>.+)$", self.add),
            (r"^(what should (i|we) watch|watch suggestions|suggest (something|a movie|a show|something to watch)|something to watch|movie night|what to watch)( tonight)?$", self.watch_cmd),
            (r"^(continue watching|on deck|what was i watching)$", self.watch_cmd),
            (r"^((media )?clean ?up( suggestions)?|free up (some )?(disk )?space|what can i delete|clean up (plex|media|the library))$", self.cleanup_cmd),
            (rf"^(delete|remove) (the )?(?P<kind>movie|film|show|series) (?P<q>.+?)( from (the )?(library|plex|disk))?$", self.delete_media_cmd),
            (r"^keep (?P<q>.+)$", self.keep_media_cmd),
            (rf"^(request|ask for) (the )?(?P<kind>movie|film|show|series) (?P<q>.+?)( for (?P<who>[\w ]+))?$", self.request_media_cmd),
            (r"^(requests|pending( requests)?|overseerr|media requests)$", self.requests),
            (r"^(?P<act>approve|decline|deny|reject) (request )?(?P<n>\d+|all)$", self.decide),
            (r"^(refresh|scan|update) plex( (library )?(?P<lib>.+))?$", self.refresh_plex),
            (r"^(indexers|check indexers|prowlarr)$", self.indexers),
            (r"^test indexers$", self.test_indexers),
        ]
        self.rules = [(re.compile(p, re.IGNORECASE), f) for p, f in self.rules]
        if self.family:
            self.rules = [(p, f) for p, f in self.rules if f.__name__ in FAMILY_COMMANDS]
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
        if self.family:
            return "Sorry, that's not something I can do for you. Say 'help' to see what I can."
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
               'destructive' deletes, reboots, stops things: always asks, whatever the mode
               'outward'     sends something in your name (an email): always asks, whatever the mode"""
        from .audit import audit
        mode = self.mode
        if self.family and level in ("destructive", "outward", "change"):
            what = doing or question.splitlines()[0].split("?")[0]
            audit(f"BLOCKED (family account {self.user.get('username')}): {what}")
            raise Blocked("That's something only the owner can do.")
        if mode == "read-only" and level != "read":
            what = doing or question.splitlines()[0].split("?")[0]
            audit(f"BLOCKED (read-only mode): {what}")
            raise Blocked(f"Read-only mode is on, so this was blocked: {what}. Say 'auto mode' or 'ask mode' to let me make changes.")
        if level == "outward":
            return self._asked(self.confirm("[sends] " + question))
        if level == "destructive":
            from . import totp
            if totp.enabled(self.cfg):
                return self._asked(self._confirm_with_code("[destructive] " + question))
            return self._asked(self.confirm("[destructive] " + question))
        if mode == "ask" and level in ("change", "read"):
            return self._asked(self.confirm(question))
        if level == "change":
            what = doing or question.splitlines()[0].split("?")[0]
            self.say(f"{what} (auto mode: not destructive, so I didn't ask)")
            audit(f"AUTO-approved: {what}")
        return True

    def _confirm_with_code(self, question: str) -> bool:
        """Two-factor approval: yes plus a code from the authenticator app. Three tries."""
        from . import totp
        from .audit import audit
        note = ""
        for attempt in range(3):
            yes, code = self.confirm_code(question + note)
            if not yes:
                return False
            if totp.verify(code):
                return True
            note = "\n  That code didn't work. Try the current one from your authenticator app."
        audit(f"2FA: three wrong codes, declined: {question.splitlines()[0][:120]}")
        return False

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
        if self.family:
            return FAMILY_HELP
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

    def discover_media(self, m) -> str:
        from . import media_discovery
        self.say("Looking for media apps in Docker containers and on your Proxmox guests...")
        r = media_discovery.scan(self.cfg)
        if not r["found"]:
            tail = ("\n" + "\n".join(r["notes"])) if r["notes"] else ""
            return "I didn't find any media apps. Is Proxmox set up, and are your Docker hosts added (VMs / servers, Unraid)?" + tail
        lines = []
        for f in r["found"]:
            state = ("already in Jarvis" if f["configured"] else "can't reach it" if not f["url"]
                     else "key found, ready to add" if f["key_found"] else f"needs its {f['needs']}" if f["needs"] else "ready to add")
            lines.append(f"  {f['label']}: {f['url'] or '-'} ({f['where']}): {state}")
        new = sum(1 for f in r["found"] if not f["configured"] and f["url"])
        tail = f"\nAdd them in Setup > Find media apps automatically ({new} new)." if new else ""
        if r["notes"]:
            tail += "\nCouldn't check: " + "; ".join(r["notes"])
        return f"Found {len(r['found'])} media app(s):\n" + "\n".join(lines) + tail

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

    # ------------------------------------------------------------ planner: reminders, notes, calendar

    def remind(self, m) -> str:
        from . import organizer as O
        when, repeat, what = O.parse_when(m.m["rest"])  # m.m keeps the original case of what to remember
        if not when:
            return "When should I remind you? For example: remind me in 20 minutes to check the oven, or remind me tomorrow at 9 to call mum."
        what = re.sub(r"^(to|that|about)\s+", "", what, flags=re.I) or "Reminder"
        rid = O.add_reminder(what, when, repeat, user=self.uid)
        rep = f", repeating {repeat}" if repeat else ""
        return f"OK, I'll remind you to {what} {when:%a %d %b at %H:%M}{rep}. (Reminder {rid})"

    def list_reminders(self, m) -> str:
        from datetime import datetime
        from . import organizer as O
        rs = O.reminders(user=self.uid)
        if not rs:
            return "No reminders set."
        return "Reminders:\n" + "\n".join(f"  {r['id']}. {r['text']}: {datetime.fromisoformat(r['due']):%a %d %b %H:%M}"
                                          + (f" ({r['repeat']})" if r["repeat"] else "") for r in rs)

    def cancel_reminder(self, m) -> str:
        from . import organizer as O
        return "Cancelled." if O.cancel_reminder(int(m["n"]), user=self.uid) else f"There's no reminder {m['n']}."

    def add_note(self, m) -> str:
        from . import organizer as O
        nid = O.add_note(m.m["text"], user=self.uid)
        return f"Noted. (Note {nid})"

    def list_notes(self, m) -> str:
        from . import organizer as O
        ns = O.notes(limit=15, user=self.uid)
        return ("Latest notes:\n" + "\n".join(f"  {n['id']}. {n['text']}" for n in ns)) if ns else "No notes yet. Say: note, then what to remember."

    def find_notes(self, m) -> str:
        from . import organizer as O
        ns = O.notes(m.m["q"], limit=8, user=self.uid)
        return ("\n".join(f"  {n['id']}. {n['text']} ({n['created'][:10]})" for n in ns)) if ns else f"Nothing in your notes about {m.m['q']}."

    def agenda(self, m) -> str:
        from . import organizer as O
        days = int(m["days"] or 7)
        items = O.upcoming(days)
        if not items:
            return f"Nothing in the next {days} days."
        return f"Next {days} days:\n" + "\n".join("  " + O.describe(i) for i in items)

    def bills(self, m) -> str:
        from . import organizer as O
        items = [i for i in O.upcoming(31) if i["kind"] == "bill"]
        if not items:
            return "No bills due in the next month."
        return "Bills:\n" + "\n".join("  " + O.describe(i).replace("Bill: ", "") for i in items)

    def add_bill(self, m) -> str:
        from . import organizer as O
        rest = m.m["rest"]
        amt = re.search(r"(?:[$£€₹]\s?)?(\d[\d,]*(?:\.\d{1,2})?)(?!\s*(?:st|nd|rd|th|am|pm|:|-))", rest)
        amount = float(amt.group(1).replace(",", "")) if amt else None
        if amt:
            rest = rest.replace(amt.group(0), " ", 1)
        when, repeat, title = O.parse_when(re.sub(r"\bdue\b", " ", rest, flags=re.I))
        title = re.sub(r"\b(for|of)\s*$", "", title).strip() or "Bill"
        if not when:
            return "When is it due? For example: add bill rent 1200 on the 1st monthly."
        eid = O.add_event(title, when.date(), "bill", amount=amount, repeat=repeat)
        return f"Added bill {title}" + (f" ({O.money(amount)})" if amount else "") + f", due {when:%a %d %b}" + (f", {repeat}" if repeat else "") + \
            f". I'll remind you 3 days before and on the day. (Bill {eid})"

    def add_event(self, m) -> str:
        from . import organizer as O
        when, repeat, title = O.parse_when(m.m["rest"])
        if not when:
            return "When is it? For example: add event dentist on 5 December at 14:30."
        timed = bool(re.search(r"\d(:\d\d)?\s*(am|pm)|\d:\d\d|\bat\b|morning|evening|tonight", m.m["rest"], re.I))
        eid = O.add_event(title or "Event", when.date(), "event", time=when.strftime("%H:%M") if timed else "", repeat=repeat)
        return f"Added {title or 'the event'} on {when:%a %d %b}" + (f" at {when:%H:%M}" if timed else "") + (f", {repeat}" if repeat else "") + f". (Event {eid})"

    def mark_paid(self, m) -> str:
        from . import organizer as O
        try:
            return O.mark_paid(m.m["name"].strip())
        except ValueError as e:
            return str(e)

    def email_summary(self, m) -> str:
        from email.utils import parseaddr
        from . import mail
        from .organizer import money
        if not self.cfg.get("email"):
            return "No email accounts set up yet: Setup > Email accounts."
        rows = mail.recent(int(m["hours"] or 24))
        if not rows:
            states = mail.states()
            errs = [f"{s['account']}: {s['error']}" for s in states if s.get("error")]
            return "No new email in the last day." + ("\nProblems: " + "; ".join(errs) if errs else "")
        imp = [r for r in rows if r["important"]]
        bills = [r for r in rows if r["bill"] and not r["bill_state"]]
        who = lambda r: parseaddr(r["sender"])[0] or parseaddr(r["sender"])[1]
        out = [f"{len(rows)} new emails in the last day: {len(imp)} important, {len(bills)} with bills."]
        if imp:
            out.append("Important:")
            out += [f"  {r['id']}. {who(r)}: {r['summary']}" for r in imp[:10]]
        if bills:
            out.append("Bills found (say 'add bill from email <number>'):")
            for r in bills[:10]:
                b = r["bill"]
                amt = f" {b.get('currency') or ''} {money(b['amount'])}".replace("  ", " ").rstrip() if b.get("amount") else ""
                out.append(f"  {r['id']}. {b.get('payee') or who(r)}{amt}" + (f", due {b['due_date']}" if b.get("due_date") else ""))
        counts = {}
        for r in rows:
            if not r["important"] and not r["bill"]:
                counts[r["category"]] = counts.get(r["category"], 0) + 1
        if counts:
            out.append("Everything else: " + ", ".join(f"{n} {c}" for c, n in sorted(counts.items(), key=lambda x: -x[1])) + ".")
        return "\n".join(out)

    def packages_cmd(self, m) -> str:
        from . import mail
        return mail.describe_packages()

    def subscriptions_cmd(self, m) -> str:
        from . import mail
        return mail.subscription_audit()

    def cancelled_cmd(self, m) -> str:
        from . import mail
        q = m["name"].lower().strip()
        for s in mail.subscriptions():
            if q in s["service"].lower() or s["service"].lower() in q:
                mail.set_subscription(s["id"], True)
                return f"Marked {s['service']} as cancelled. I'll leave it out of the audit."
        return f"I don't have a subscription called {m['name']}. Say 'subscriptions' to see the list."

    def send_email(self, to: str, subject: str, body: str, mail_id: int | None = None) -> str:
        """Send an email in the user's name. Always shows the exact email and asks, in every mode."""
        from email.utils import parseaddr
        from . import mail
        in_reply_to = None
        body, subject, to = mail._plain_text(body or ""), mail._plain_text(subject or ""), (to or "").strip()
        if re.match(r"(hi|hello|hey|dear)\b", subject, re.I):
            subject = ""  # a greeting isn't a subject
        if not mail_id and to and "@" not in to and not subject:
            # writing to someone who emailed recently with no new subject: answer in their thread
            recent = [r for r in mail.recent(24 * 14) if to.lower() in parseaddr(r["sender"])[0].lower()]
            if recent:
                mail_id = recent[0]["id"]
        orig = None
        if mail_id:
            orig = mail.mail_row(int(mail_id))
            if not orig:
                return f"I can't find email {mail_id}."
            to = to or orig.get("reply_to") or parseaddr(orig["sender"])[1]
            in_reply_to = orig.get("message_id")
            if not subject:
                subject = orig["subject"] if orig["subject"].lower().startswith("re:") else "Re: " + orig["subject"]
        address = mail.find_contact(to or "")
        if not address:
            return f"I don't have an email address for {to}. Give me the address, e.g. 'email name@example.com saying ...'."
        try:
            frm = mail.sending_account(self.cfg)["address"]
        except mail.MailError as e:
            return str(e) + "."
        body = body.strip()
        subject = (subject or body.split("\n")[0][:60]).strip()
        warn = ""
        if orig and orig["category"] in ("spam", "promotion"):
            warn = f"\nCareful: this answers a message the screening marked as {orig['category']}. Emails can try to trick me into sending things."
        elif mail.times_heard_from(address) <= 1:
            warn = "\nCareful: you've had no other email from this address. Emails can try to trick me into sending things."
        preview = f"Send this email?{warn}\nFrom: {frm}\nTo: {address}\nSubject: {subject}\n\n{body}"
        if not self.allow(preview, "outward", doing=f"Email to {address}"):
            return "Not sent."
        from .audit import audit
        try:
            sent_from = mail.send(self.cfg, address, subject, body, in_reply_to=in_reply_to)
        except mail.MailError as e:
            return str(e)
        audit(f"EMAIL SENT from {sent_from} to {address}: {subject}")
        return f"Sent to {address}."

    def reply_cmd(self, m) -> str:
        from . import mail
        who = m["who"].strip()
        if who.isdigit():
            return self.send_email("", "", m.m["body"], mail_id=int(who))
        rows = [r for r in mail.recent(24 * 30) if who.lower() in r["sender"].lower()]
        if not rows:
            return f"I can't find a recent email from {who} to reply to."
        return self.send_email("", "", m.m["body"], mail_id=rows[0]["id"])

    def email_cmd(self, m) -> str:
        return self.send_email(m.m["who"].strip(), "", m.m["body"])

    def email_bill(self, m) -> str:
        from . import mail
        try:
            return mail.bill_to_calendar(int(m["n"]))
        except ValueError as e:
            return str(e)

    # ------------------------------------------------------------ lists, memory, habits, weather

    def list_add_items(self, name: str, items: list) -> str:
        from . import life
        added = life.add_items(name, items)
        lst = life.list_name(name)
        if not added:
            return f"Those are already on the {lst} list."
        return f"Added {', '.join(added)} to the {lst} list ({len(life.items(lst))} on it now)."

    def list_add(self, m) -> str:
        from . import life
        return self.list_add_items(m.m["name"], life.split_items(m.m["items"]))

    def show_list(self, m) -> str:
        from . import life
        if not m["name"]:
            ls = life.lists()
            return ("Your lists: " + ", ".join(f"{n} ({c})" for n, c in ls.items())) if ls else "All your lists are empty."
        lst = life.list_name(m["name"])
        its = life.items(lst)
        return (f"{lst.title()} list:\n" + "\n".join(f"  {i['text']}" for i in its)) if its else f"The {lst} list is empty."

    def list_check(self, name, item: str) -> str:
        from . import life
        done = [t for t in (life.check(name, it) for it in life.split_items(item)) if t]
        return f"Ticked off {', '.join(done)}." if done else f"I couldn't find {item} on {'the ' + life.list_name(name) if name else 'your'} lists."

    def list_check_cmd(self, m) -> str:
        return self.list_check(m.m["name"] if m["name"] else None, m.m["item"])

    def list_remove(self, m) -> str:
        from . import life
        t = life.remove(m.m["name"], m.m["item"])
        return f"Removed {t}." if t else f"{m.m['item']} isn't on the {life.list_name(m.m['name'])} list."

    def list_clear(self, m) -> str:
        from . import life
        n = life.clear_done(m.m["name"] if m["name"] else None)
        return f"Cleared {n} ticked-off item{'s' if n != 1 else ''}."

    def remember_fact(self, fact: str) -> str:
        from . import life
        mid = life.remember(fact)
        return f"I'll remember that. (Memory {mid})"

    def remember_cmd(self, m) -> str:
        return self.remember_fact(m.m["fact"])

    def list_memories(self, m) -> str:
        from . import life
        ms = life.memories()
        return ("What I remember:\n" + "\n".join(f"  {x['id']}. {x['text']}" for x in ms)) if ms else "You haven't asked me to remember anything yet."

    def forget_fact(self, what: str) -> str:
        from . import life
        gone = life.forget(what)
        return f"Forgotten: {'; '.join(gone)}" if gone else f"I don't remember anything about {what}."

    def forget_cmd(self, m) -> str:
        return self.forget_fact(m.m["what"])

    def habit_add(self, name: str, time: str, days: str = "daily") -> str:
        from . import life
        hid = life.add_habit(name, time, days)
        return f"I'll check in on {name} at {time} ({days}). Say 'done {name}' when it's done. (Habit {hid})"

    def habit_add_cmd(self, m) -> str:
        from . import organizer as O
        rest = m.m["rest"]
        days = "weekdays" if re.search(r"\bweekdays?\b", rest, re.I) else "weekends" if re.search(r"\bweekends?\b", rest, re.I) else "daily"
        rest = re.sub(r"\b(every ?day|daily|on weekdays|weekdays?|on weekends|weekends?)\b", " ", rest, flags=re.I)
        when, _, name = O.parse_when(rest)
        if not when:
            return "At what time should I check in? For example: add habit take meds at 21:00."
        return self.habit_add(name or "habit", when.strftime("%H:%M"), days)

    def habit_done(self, m) -> str:
        from . import life
        try:
            raw = getattr(m, "m", None)
            return life.mark_habit(raw["name"] if isinstance(raw, re.Match) else m["name"])
        except ValueError as e:
            return str(e)

    def habit_list(self, m) -> str:
        from . import life
        hs = life.habits()
        if not hs:
            return "No habits yet. Try: add habit take meds at 21:00."
        return "Habits:\n" + "\n".join(f"  {h['name']} at {h['time']} ({h['days']}): " + ("done today" if h["done_today"] else "not done yet" if h["due_today"] else "not today") +
                                       (f", {h['streak']}-day streak" if h["streak"] > 1 else "") for h in hs)

    def weather(self, m) -> str:
        from . import weather as W
        loc = self.cfg.get("location") or {}
        if not loc.get("home"):
            return "Set your location first: Setup > Weather & commute."
        day = m["day"] if m["day"] is not None else (1 if (m["when"] or "") == "tomorrow" else 0)
        units = loc.get("units") or "metric"
        try:
            return W.describe_day(W.forecast(loc["home"], units), int(day), units)
        except W.WeatherError as e:
            return str(e)

    # ------------------------------------------------------------ security and network

    def security_digest(self, m) -> str:
        from . import security
        return security.digest(self.cfg, self.reports_source() if getattr(self, "reports_source", None) else None)

    def open_ports(self, m) -> str:
        from . import security
        try:
            ports = security.open_ports(self.cfg)
        except Exception as e:
            return f"Couldn't read the port forwards from pfSense: {e}"
        if ports is None:
            return "pfSense isn't set up in Jarvis."
        return ("Open to the internet (pfSense port forwards):\n" + "\n".join(f"  {p}" for p in ports)) if ports else "Nothing is forwarded from the internet. Good."

    def internet_speed(self, m) -> str:
        from . import network
        self.say("Running a speed test (about 35 MB)...")
        try:
            r = network.speed_test()
        except Exception as e:
            return f"The speed test failed: {e}"
        hist = network.history()
        usual = sorted(x["down"] for x in hist[-15:])[len(hist[-15:]) // 2] if len(hist) >= 3 else None
        return f"{r['down']} Mbps down, {r['up']} Mbps up, {r['ping']} ms." + (f" Usually about {usual:.0f} Mbps down." if usual else "")

    def public_ip(self, m) -> str:
        from . import network
        try:
            return f"Your public IP is {network.public_ip()}."
        except Exception as e:
            return str(e)

    def name_device(self, m) -> str:
        from . import network
        devices = []
        try:
            from .collectors.pfsense import PfSenseCollector
            for e in self.cfg.get("pfsense") or []:
                devices += PfSenseCollector(e, self.cfg["thresholds"]).collect_devices()
        except Exception:
            pass
        try:
            mac = network.name_device(m.m["who"], m.m["label"], devices)
        except ValueError as e:
            return str(e)
        return f"OK, {mac} is now called {m.m['label'].strip()}."

    # ------------------------------------------------------------ documents and research

    def ask_documents(self, m) -> str:
        from . import docs
        raw = getattr(m, "m", None)
        q = raw["q"] if isinstance(raw, re.Match) else m["q"]
        self.say("Looking through your documents (local AI)...")
        try:
            r = docs.ask(self.cfg, q)
        except docs.DocsError as e:
            return str(e)
        src = "; ".join(f"[{s['n']}] {s['path']}" + (f", page {s['page']}" if s["page"] and str(s["path"]).endswith(".pdf") else "") for s in r["sources"])
        return r["answer"] + (f"\nSources: {src}" if src else "")

    def list_documents(self, m) -> str:
        from . import docs
        ds = docs.documents()
        if not ds:
            return "No documents yet. Upload them in Planner > Documents, or drop files into config/data/documents."
        return f"{len(ds)} documents:\n" + "\n".join(f"  {d['title']} ({d['kind']}" + (f", {d['pages']} pages" if d["pages"] and d["pages"] > 1 else "") + ")"
                                                   + (f" - couldn't read: {d['error']}" if d["error"] else "") for d in ds[:40])

    def web_search(self, m) -> str:
        from . import search
        raw = getattr(m, "m", None)
        q = raw["q"] if isinstance(raw, re.Match) else m["q"]
        try:
            res = search.web_search(self.cfg, q)
        except search.SearchError as e:
            return str(e)
        if not res:
            return f"No results for {q}."
        return f"Top results for {q}:\n" + "\n".join(f"  {r['title']}\n    {r['url']}" for r in res[:6])

    # ------------------------------------------------------------ location

    def remind_at_place(self, m) -> str:
        from . import location
        raw = m.m
        ev = raw["ev"].lower()
        place = (raw["place"] or "").strip(" .")
        if "home" in ev and not place:
            place = "home"
        event = "leave" if ev.startswith("leave") else "arrive"
        if not place:
            return "Which place? For example: remind me to call mum when I leave work."
        try:
            return location.add_location_reminder(raw["what"], place, event)
        except ValueError as e:
            return str(e)

    def where_am_i(self, m) -> str:
        from datetime import datetime
        from . import location
        p = location.latest()
        if not p:
            return "No location yet. Add a phone on the Places page (OwnTracks or the Jarvis app)."
        pl = location.place_at(p["lat"], p["lon"])
        ago = (datetime.now().timestamp() - p["t"]) / 60
        when = "just now" if ago < 2 else f"{ago:.0f} min ago" if ago < 120 else datetime.fromtimestamp(p["t"]).strftime("%a %H:%M")
        return (f"At {pl['name']}" if pl else f"Somewhere I don't have a name for ({p['lat']:.4f}, {p['lon']:.4f})") + f", as of {when} ({p['tracker']})."

    def where_was_i_cmd(self, m) -> str:
        from datetime import datetime, timedelta
        from . import location, organizer
        raw = getattr(m, "m", None)
        text = raw["when"] if isinstance(raw, re.Match) else m["when"]
        when, _, _ = organizer.parse_when(text)
        if not when:
            return "When? For example: where was I yesterday at 3pm."
        while when > datetime.now() + timedelta(minutes=5):
            when -= timedelta(days=7)  # 'on tuesday' means the last one
        return location.where_was_i(when)

    def how_far(self, m) -> str:
        from datetime import date, timedelta
        from . import location
        span = m["span"] or "today"
        if span == "this week":
            return location.week_summary()
        return location.describe_day(date.today() - timedelta(days=1 if span == "yesterday" else 0))

    def name_here(self, m) -> str:
        from . import location
        p = location.latest()
        if not p:
            return "I don't know where you are yet."
        name = m.m["name"].strip(" .")
        kind = name.lower() if name.lower() in ("home", "work") else "other"
        location.add_place(name.title() if kind != "other" else name, p["lat"], p["lon"], kind)
        return f"Saved this place as {name}."

    def daily_report(self, m) -> str:
        from . import organizer as O
        return self.report_source() if self.report_source else O.daily_report()

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

    # ------------------------------------------------------------ watching, cleanup, family requests

    def watch_cmd(self, m, kind: str = "any", minutes: int | None = None, genre: str | None = None) -> str:
        from .media import insights
        if not self.stack.plex:
            return "Plex isn't set up, so I can't see your library."
        return insights.describe_suggestions(insights.suggestions(self.stack, kind, minutes, genre))

    def cleanup_cmd(self, m) -> str:
        from .media import insights
        try:
            return insights.describe_cleanup(insights.cleanup(self.stack, self.cfg))
        except LookupError as e:
            return str(e)

    def delete_media(self, kind: str, title: str) -> str:
        from .media import insights
        kind = KINDS.get(kind, kind)
        if kind not in ("movie", "show"):
            return "I can delete movies or shows."
        self._arr(kind)
        item = insights.find_in_library(self.stack, kind, title)
        if not item:
            return f"I can't find a {kind} called {title} in the library."
        size = item.get("sizeOnDisk") or (item.get("statistics") or {}).get("sizeOnDisk") or 0
        label = f"{item.get('title')}" + (f" ({item['year']})" if item.get("year") else "")
        if not self.allow(f"Delete {label} and its files ({insights.gb(size)})? It won't be downloaded again automatically.", "destructive",
                          doing=f"Delete {label}"):
            return "Okay, kept it."
        insights.delete_from_library(self.stack, kind, item)
        from .audit import audit
        audit(f"MEDIA DELETED: {kind} {label}, {insights.gb(size)}")
        return f"Deleted {label}, freeing {insights.gb(size)}. Plex will drop it at its next scan."

    def delete_media_cmd(self, m) -> str:
        return self.delete_media(m["kind"], m.m["q"])

    def keep_media(self, title: str) -> str:
        from .media import insights
        for kind in ("movie", "show"):
            if self.stack.for_kind(kind) and (item := insights.find_in_library(self.stack, kind, title)):
                ext = item.get("tmdbId") if kind == "movie" else item.get("tvdbId")
                insights.keep(f"{kind}:{ext}", item.get("title"))
                return f"I'll stop suggesting {item.get('title')} for cleanup."
        return f"I can't find {title} in the library."

    def keep_media_cmd(self, m) -> str:
        return self.keep_media(m.m["q"])

    def request_media(self, kind: str, title: str, who: str = "you") -> str:
        from .media import insights
        if self.family:
            who = self.user.get("name") or self.user.get("username")
        try:
            return insights.add_request(self.stack, who or "you", KINDS.get(kind, kind), title, uid=self.uid)[1]
        except LookupError as e:
            return str(e)

    def request_media_cmd(self, m) -> str:
        return self.request_media(m["kind"], m.m["q"], (m.m["who"] or "you").strip())

    def requests(self, m) -> str:
        from .media import insights
        self.last_requests = []
        lines = []
        for r in insights.requests_list("pending"):
            self.last_requests.append({"_jarvis": r["id"], "label": f"{r['title']} ({r['year'] or '?'})"})
            lines.append(f"{len(self.last_requests)}. {r['title']} ({r['year'] or '?'}) [{r['kind']}], asked for by {r['who']}")
        o = self.stack.overseerr
        if o:
            for r in o.pending():
                self.last_requests.append(r)
                lines.append(f"{len(self.last_requests)}. {o.title_of(r)} [{r.get('type', '?')}], requested by "
                             f"{(r.get('requestedBy') or {}).get('displayName', '?')} (Overseerr)")
        if not lines:
            return "No requests waiting for approval."
        return "\n".join(lines) + "\nSay 'approve 1', 'decline 2' or 'approve all'."

    def decide(self, m) -> str:
        o = self.stack.overseerr
        if not self.last_requests:
            self.requests(m)
        if not self.last_requests:
            return "No requests waiting for approval."
        approve = m["act"] == "approve"
        if m["n"] == "all":
            chosen = list(self.last_requests)
        else:
            n = int(m["n"])
            if not 1 <= n <= len(self.last_requests):
                return f"There's no request {n}. I listed {len(self.last_requests)}."
            chosen = [self.last_requests[n - 1]]
        names = ", ".join(r["label"] if "_jarvis" in r else o.title_of(r) for r in chosen)
        if not self.allow(f"{'Approve' if approve else 'Decline'} {names}?"):
            return "Okay, cancelled."
        from .media import insights
        out = []
        for r in chosen:
            if "_jarvis" in r:
                out.append(insights.decide(self.stack, r["_jarvis"], approve))
            else:
                (o.approve if approve else o.decline)(r["id"])
                out.append(f"{'Approved' if approve else 'Declined'} {o.title_of(r)} in Overseerr.")
            self.last_requests.remove(r)
        return "\n".join(out)

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
