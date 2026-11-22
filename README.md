# Jarvis: homelab assistant

Jarvis watches Proxmox, pfSense, Unraid, your VMs and your media stack directly, and you can control the media stack with text commands. You don't need Prometheus, Netdata or anything else installed. Voice and AI come next and use this as their eyes.

## What it watches

| Source | How | What |
|---|---|---|
| Proxmox | Read-only API token | Node CPU, RAM, load and uptime. Storage pools (fill and offline). SMART health and SSD wear. Every VM and LXC. Disk usage *inside* VMs (via the guest agent) |
| pfSense | REST API package, read-only key | WAN gateways (down, latency, packet loss). Interfaces (up/down, live Mbps, errors). Firewall state table. Stopped services. CPU, RAM, disk, temp. Every device on the network (IP, MAC, hostname), with **alerts for new devices**. Top blocked IPs and ports from the firewall log |
| Unraid | SSH | Array state, per-disk status, temps and errors, parity check progress. Cache, flash, docker.img and `/var/log` fill. Containers |
| Media | Each app's API key | Plex streams and transcodes. Sonarr, Radarr and Lidarr health and stuck downloads. Prowlarr failing indexers. Overseerr/Jellyseerr pending requests. qBittorrent, Deluge and SABnzbd: speeds, errored or stalled items, failed downloads, low disk space |
| Linux VMs | SSH | CPU, RAM, load, disks, failed systemd services, Docker containers |

## Quick start
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m jarvis web
```
Open **http://localhost:8765** and go to **Setup**. For each service you use, fill in the form and click **Save & test**. Jarvis saves the settings and tells you straight away whether the connection works. Prefer the terminal? `python -m jarvis setup` asks the same questions one by one (`python -m jarvis setup sonarr` does just one service).

The dashboard has these tabs:
- **Dashboard:** live health of everything, refreshed automatically, plus recent alerts.
- **Chat:** every text command, with Yes/No buttons for anything that changes something.
- **Devices:** everything pfSense sees on your network.
- **Activity:** the audit log of commands run, VMs created and IPs reserved.
- **Setup:** add, change, test or remove services.

While the dashboard runs, it also does the monitoring and sends alerts, so you don't need `monitor` running separately.

**Opening it from your phone or other PCs:** in Setup → Web dashboard, set a password and "Listen on" `0.0.0.0`, then restart `python -m jarvis web`. Jarvis refuses to listen on the network without a password, because the dashboard can run commands and create VMs.

**Where settings go:** addresses and options go in `config.yaml`. Keys and passwords go in `.env` and are never sent back to the browser. Each save keeps the previous config as `config.yaml.bak`. You can still edit both files by hand; the sections below explain each service. `config.example.yaml` shows every option.

## Run it 24/7 in Docker
One container runs everything: dashboard, monitoring, alerts, chat and AI. The code is a git checkout on the host, mounted into the container, so **Jarvis can update itself from GitHub**. All of its settings live in `./config`: `config.yaml`, `.env` (keys and passwords), `data/` and its SSH key. Updates never touch them.

**Where to run it:** a small Proxmox LXC or VM with Docker is best. If Jarvis runs on Unraid, it can't tell you when Unraid is down. Unraid works too. 1 CPU and 512 MB RAM is plenty, since the AI runs on your Ollama box.

### Install
On the Docker host, as root:
```
git clone https://github.com/gtzapper/jarvis.git /opt/jarvis && /opt/jarvis/install.sh
```
For a **private repo**, give it a GitHub token. Create a fine-grained token with read-only "Contents" access to just this repo:
```
GITHUB_TOKEN=github_pat_xxx bash -c 'git clone https://x-access-token:$GITHUB_TOKEN@github.com/gtzapper/jarvis.git /opt/jarvis && git -C /opt/jarvis remote set-url origin https://github.com/gtzapper/jarvis.git && /opt/jarvis/install.sh'
```
`install.sh` does the following:
- Checks Docker, Compose and git.
- Picks sensible defaults: `/opt/jarvis` and user 1000. On Unraid it uses `/mnt/user/appdata/jarvis` and user 99:100.
- Takes your time zone from the host.
- Saves the token so the in-app updater can pull too.
- Builds and starts the container.
- Prints the dashboard address and the **first-start password**.

You can override the defaults: `JARVIS_DIR=/srv/jarvis JARVIS_PORT=9000 TZ=Asia/Kolkata ./install.sh`.

### Updating
- **From the app:** go to Setup → **Jarvis version** → Check for updates. It lists what's new; click **Update now**. Jarvis pulls the changes, restarts itself and the page reloads on the new version. You can also just say `update jarvis` in Chat.
- **From the host:**

  | Command | What it does |
  |---|---|
  | `/opt/jarvis/update.sh` | Same as the button, with a prompt |
  | `--yes` | Update without asking |
  | `--rollback` | Go back to the previous version |
  | `--rebuild` | Rebuild the Docker image |

**Safety rules:**
- **Fast-forward only.** If any file was edited by hand on the host, or the copy has diverged from GitHub, Jarvis refuses and tells you why. It never overwrites changes.
- **Every update and rollback** is written to the Activity log.
- **New dependencies** in an update are installed automatically on restart.
- **Docker image changes** need a rebuild: if an update changes the `Dockerfile`, Jarvis says so; run `update.sh --rebuild`.
- **If an update breaks the app**, run `update.sh --rollback` on the host. That works even when the dashboard won't load.

### Bring your existing settings (optional)
To keep what you set up on this PC, copy it into the host's config folder, then restart:
```
scp "D:\Project X\config.yaml" root@DOCKER-HOST:/opt/jarvis/config/
```
Then on the host:
```
chown -R 1000:1000 /opt/jarvis/config && docker restart jarvis
```

### Proxmox LXC notes
Docker inside an LXC needs nesting. Turn it on in the container's Options → Features (`nesting=1`, plus `keyctl=1` for unprivileged containers). A small Debian VM avoids that question entirely.

### Unraid
Install the **Compose Manager** plugin first, then run the same install command from Unraid's terminal.

## Manual setup (what Setup does for you)

### 1. Install
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml
copy .env.example .env
```

### 2. Proxmox: read-only token
In the Proxmox shell (node → Shell):
```
pveum user add jarvis@pve
pveum aclmod / -user jarvis@pve -role PVEAuditor
pveum user token add jarvis@pve monitor --privsep 0
```
Put the token ID (`jarvis@pve!monitor`) and the secret it prints into `.env`. PVEAuditor can *read* everything and change nothing.

Optional, to see disk usage inside VMs: install `qemu-guest-agent` in each VM, enable "QEMU Guest Agent" in the VM's Options, then grant:
```
pveum role add JarvisAgent -privs VM.GuestAgent.Audit
pveum aclmod /vms -user jarvis@pve -role JarvisAgent
```
(On Proxmox 8, use `VM.Monitor` instead of `VM.GuestAgent.Audit`.)

### 3. pfSense: REST API with a read-only key
1. **Install the package.** In pfSense, go to Diagnostics → Command Prompt (or SSH, option 8) and run:
   ```
   pkg-static add https://github.com/pfrest/pfSense-pkg-RESTAPI/releases/latest/download/pfSense-2.8.1-pkg-RESTAPI.pkg
   ```
   You must re-run this after every pfSense upgrade. Package upgrades remove it.
2. **Create a limited user.** In System → User Manager → Add, create user `jarvis` with a long random password. Leave it out of the admins group.
3. **Give it read-only privileges.** Edit the user → Effective Privileges → Add. Search for each path below and pick only its **GET** privilege:
   - `/api/v2/status/system`, `/api/v2/system/version`
   - `/api/v2/status/gateways`, `/api/v2/status/interfaces`, `/api/v2/status/services`
   - `/api/v2/firewall/states/size`, `/api/v2/status/logs/firewall`
   - `/api/v2/diagnostics/arp_table`, `/api/v2/status/dhcp_server/leases`

   Also add **"User - Config: Deny Config Write"** as an extra safety net. If you miss one, `status` will tell you which section needs it.
4. **Create the key as that user.** Log out, log in as `jarvis`, then go to System → REST API → Keys → Add. The key inherits `jarvis`'s privileges. Put it in `.env` as `PFSENSE_API_KEY`.

The first run learns every device already on your network. After that, only new devices are announced. Phones with "private Wi-Fi address" turned on can show up as new now and then; Jarvis labels those.

### 4. SSH key for Unraid and VMs
On this PC:
```
ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\jarvis_ed25519 -N ""
```
- **Unraid:** Users → root → paste the contents of `jarvis_ed25519.pub` into "SSH authorized keys".
- **VMs:** create a normal user (`sudo adduser jarvis`), add the key to `~jarvis/.ssh/authorized_keys`, and if the VM runs Docker, `sudo usermod -aG docker jarvis`. Root isn't needed.

Jarvis only runs read-only commands (`/proc`, `df`, `docker ps`, `systemctl --failed`, Unraid's status files). Host keys are pinned in `data/known_hosts` on first connect.

**Password login instead of a key:** set "Login with" to `password` on a host in Setup, or pick Password next to Add in the discovered list, and enter the user's password. It's saved in `.env` like every other secret and never shown again. Keys are still recommended:
- A password sits in a file that Jarvis can read.
- Many Linux cloud images turn password SSH off. `PasswordAuthentication no` in `/etc/ssh/sshd_config` blocks it.
- `sudo` commands from `run ... on` won't be able to prompt for a password. Use root, or give the user passwordless sudo for what you need.

Windows' OpenSSH Server allows password logins by default.

**Discovery defaults:** at the top of Setup → Discovered on Proxmox, "When adding, try:" sets how each Add is pre-filled:
- **Username = the guest's own name** (so a VM called `plex` is tried as user `plex`), or a single fixed user for all guests.
- **Default login** (key or password) and a **default password**, saved once in `.env` as `DISCOVERY_PASSWORD`. When you add a guest with password login and leave the box blank, Jarvis tries this one. Every host that uses it references the same saved password, so changing it in one place updates them all.

You can still override the username, login type or password on any individual row before clicking Add.

### 5. Media stack
For each app you run, add its URL under `media:` in `config.yaml` and its key in `.env`. Leave out anything you don't use.
- **Sonarr, Radarr, Lidarr, Prowlarr, Overseerr/Jellyseerr:** Settings → General → API Key.
- **qBittorrent:** your Web UI username, with the password in `.env`. Or whitelist Jarvis's IP under Web UI → "Bypass authentication for clients in whitelisted IP subnets" and leave the password out.
- **Deluge:** the web UI password (default `deluge`).
- **SABnzbd:** Config → General → API Key.
- **Plex token:** in Plex Web, open any movie → ⋯ → Get Info → View XML. The address bar of the page that opens ends in `X-Plex-Token=...`.

Optionally set `quality_profile` and `root_folder` per app. Otherwise Jarvis uses the first one each app has.

### 6. Edit `config.yaml`
Fill in your IPs, and list anything that must always be up (`must_run` VM IDs, `must_run_containers`).

### 7. Run
```
python -m jarvis status            # full report
python -m jarvis status --brief    # one sentence, e.g. "All 4 systems are running normally."
python -m jarvis monitor           # keep watching and alert on problems
python -m jarvis devices           # every device pfSense sees
python -m jarvis devices 192.168.1.47   # "what is this IP?"
python -m jarvis test-notify       # check phone push works
```

## Text commands
```
python -m jarvis chat                      # interactive
python -m jarvis do "what's downloading"   # one-shot
```
| Say | What happens |
|---|---|
| `status` / `media status` | One-line health of the whole lab, or a media breakdown |
| `downloads`, then `remove download 2` | Numbered queue across all *arrs. Remove drops a stuck download and blocklists the release so a different one is grabbed |
| `torrents` / `speeds` / `usenet` | Every download client's speed and unfinished items, numbered |
| `pause downloads`, `resume downloads` | Everything at once, or one client: `pause qbittorrent`, `resume sab` |
| `pause 3` / `resume 3` | One item from the last `torrents` list |
| `slow mode on` / `full speed` | Throttle every client (qBittorrent alt speed limits, Deluge cap, SABnzbd %), e.g. while gaming |
| `who's watching` / `recent` | Plex streams (with transcode info) / recently added |
| `upcoming 14 days` | Episodes, movies and albums coming up |
| `add movie dune`, `add show severance`, `add artist radiohead` | Look it up, let you pick the right match, add it and start the search |
| `search movie dune` | Look up without adding |
| `search missing shows` | Tell Sonarr (or Radarr/Lidarr) to hunt for everything missing |
| `requests`, then `approve 2` / `decline 1` / `approve all` | Overseerr approvals |
| `refresh plex tv` | Scan a Plex library (or all of them) |
| `indexers` / `test indexers` | Prowlarr indexer health |

What asks first depends on the permission mode (see below). `do --yes` skips the question and takes the top match, which is handy for scripts. Free-form English ("grab that new dune movie") comes with the AI brain stage, which will call these same commands.

## AI brain (talk in plain English)
With the AI brain set up, Jarvis understands normal sentences and works out which of its tools to use:
```
you> free up some space on docker-vm
     • Checking what's using the disk on docker-vm.
     • Running on docker-vm: ncdu -o- /
         docker-vm: exit 127: ncdu: not found
     • ncdu isn't installed, so I'll use du instead.
     • Running on docker-vm: du -sh /var/* | sort -h | tail
     • Docker images are the big one. Clearing unused ones.
       [Destructive, so I always ask: Remove unused Docker images...? -> Yes]
jarvis> Freed 9 GB on docker-vm: Docker images were taking 12 GB, so I cleaned the unused ones.

you> something's stuck in sonarr, sort it out
you> grab the 2021 dune and the new severance season
you> is anyone transcoding on plex right now?
you> slow the downloads down, I'm gaming
```
**Setup:** create an API key at console.anthropic.com → API Keys, then paste it into Setup → **AI brain (Claude)** and click Save & test. It's stored in `.env` as `ANTHROPIC_API_KEY`.

**How it behaves:**
- **You say what you want; it does the steps.** It works out the commands itself, looks before it changes things, and checks the result afterwards.
- **It recovers from failures.** If a command fails (not installed, permission denied, wrong container name), it reads the error and tries another way, up to 3 different attempts, before telling you what's still wrong.
- **It narrates as it goes.** Each step appears live in the chat ("Checking the logs for sonarr..."), including why a step failed.
- **Everything goes through the AI** when it's set up, using the same commands you can type. `help`, `new chat` and the mode commands still answer instantly. If the AI can't be reached, exact commands like `downloads` still work the old way.
- **The permission mode decides what asks first** (see below). The AI can't skip that gate, and everything is written to the audit log.
- **It remembers the conversation.** "Remove that one" works. Say `new chat` (or tap it) to start fresh.
- **Data stays data.** Text from your systems (torrent names, logs, command output) is treated as information. Instructions hidden inside it are ignored.

**Cost:** you pay Anthropic per use. A typical question with a couple of tool calls costs a few cents. To make it cheaper, set Effort to `low` in Setup. Model `claude-opus-5-5` is the default. The dashboard's `/api/ai` endpoint shows token usage since start.

## Planner: reminders, notes, events, bills and the daily report
Tell Jarvis in the chat, by voice, or use the **Planner** page in the dashboard and the app:
```
remind me in 20 minutes to check the oven
remind me to take the bins out every monday at 7pm
note: the NAS fan is a Noctua NF-A12x25        (find notes nas fan / what did i note about plex)
add bill rent 1200 on the 1st monthly           (bills / mark rent paid)
add event dentist on 5 December at 14:30        (agenda / what's coming up 30 days)
daily report
```
With the AI brain on, plain English works too ("remind me the day before the electricity bill is due"), and it can look things up in your notes.

- **Reminders** ring at the time you set; repeating ones (daily, weekdays, weekly, monthly, yearly) move on by themselves.
- **Bills** remind you 3 days before (you can change it per bill) and on the day. An unpaid bill shows as overdue. **Paid** moves a repeating bill to its next due date.
- **Events** with a time get a heads-up 30 minutes before.
- **Daily report** at the time you pick (default 08:00): lab health, recent alerts, today's calendar and bills due this week. Ask for it any time with `daily report`.

**Where they reach you** (Setup → Reminders & daily report, each can be switched off):
- **ntfy push:** instant, needs Phone alerts (ntfy).
- **Jarvis app:** reminders are set as exact alarms on the phone, so they ring on the minute. Bill notices and the daily report arrive within 15 minutes.
- **Listening devices:** speak them aloud, except during quiet hours (default 22:00–07:00).

**Publish to your phone's calendar** (off until you turn it on):
- **Android with the Jarvis app:** Planner tab → **Show in phone calendar**. It adds a "Jarvis" calendar to your phone's calendar app, filled with the next 90 days of events, bills and reminders, and refreshes on each sync. No link, no extra app, works on your home network or VPN.
- **iPhone / Mac:** dashboard → Planner → **Publish to phone calendar** → scan the QR code with the camera (or tap **Add to iPhone / Mac Calendar**) and confirm Subscribe.
- **Android without the app:** turn on Publish, install the free ICSx⁵ app, and scan the QR code. Google Calendar's own "From URL" option fetches from Google's servers, which can't reach your home network.

The link is private: anyone who has it can read your calendar. **New link** replaces it, and switching Publish off stops it working entirely.

Everything is stored in `data/organizer.db` on the Jarvis host.

## Lists, memory, habits and weather
```
add milk, eggs and bread to the shopping list     (shopping list / got milk / clear done items)
add call the plumber to my todo list               (any name works: "add passport to the packing list")
remember that the guest wifi password is on the fridge     (what do you remember / forget wifi)
add habit take meds at 21:00                        (add habit stretch at 7am on weekdays / done meds / habits)
weather / weather tomorrow / will it rain
```
- **Lists** are in the dashboard (Planner → Lists) and the app (Planner → Lists). Tick things off there or by voice.
- **Memory:** facts you ask Jarvis to remember are given to the AI in every conversation, so it can use them. They're stored as facts from you, not as instructions. Planner → "What Jarvis remembers" shows them, and you can forget any of them.
- **Habits:** Jarvis checks in at the time you pick, through your reminder channels. If it's still not done, there's one nudge 3 hours later (by 22:00 for evening habits). Streaks are kept, and the daily report says how yesterday went.
- **Weather and commute** (Setup → Weather & commute, using Open-Meteo, free and with no account): the forecast with a rain warning goes in the daily report and on the Dashboard. On weekdays the report also includes a typical drive time to work (no live traffic).

## Backup watchdog and expiry warnings
- **Backups** (on when Proxmox or Unraid is set up; Setup → Backup watchdog):
  - Every Proxmox guest in a backup job must have a backup newer than 30 hours (configurable).
  - Failed backup jobs are reported.
  - Running guests that aren't in any backup job are listed.
  - ZFS pools on Proxmox nodes must be scrubbed regularly and without errors.
  - Unraid parity checks must run regularly and without errors.
  - It all appears on the Dashboard and alerts like any other problem.
- **Certificates & domains** (Setup → Certificates & domains): list your HTTPS services (self-signed certificates work too) and domains. Jarvis warns 21 days before they expire, and again at 7 days.

## Security watch
- **SSH login watch** (Setup → Login watch & security digest; on by default):
  - Reads the SSH login log on every Linux and Unraid machine Jarvis can log into, and alerts on a login from an address it hasn't seen before. Jarvis's own logins and addresses you trust are ignored.
  - Alerts on bursts of failed logins (10 in half an hour by default), and on new user or admin (sudo) accounts.
  - Reading the log needs root or passwordless sudo for the Jarvis user.
- **Weekly security digest** (Sunday 10:00 by default, or `security digest` any time): logins and new source addresses, failed attempts, new accounts, new devices, public IP changes, what's open to the internet (pfSense port forwards) and any open backup or certificate problems.
- **Internet watch:** the public IP is checked every 15 minutes, with an alert when it changes. A speed test runs through Cloudflare every 12 hours (about 35 MB each, configurable or off), with a warning when the internet is much slower than usual. Chat: `internet speed`, `public ip`, `open ports`.
- **Device names:** `name device 10.0.20.44 as Living room TV`. The name shows on the Devices page, and new-device alerts tell you how to name them.
- **Two-factor approval** (Setup → Two-factor approval): destructive actions also need the 6-digit code from an authenticator app (Google Authenticator, Aegis, 1Password, Bitwarden...). Scan the QR code, enter the first code, done.
  - Each code works once.
  - Three wrong codes decline the action and are written to the audit log.
  - Turning it off needs a current code.
  - Keep the key safe: if you lose the phone, you'll need it, or you can set `two_factor: {enabled: false}` in config.yaml.

## Ask your documents, receipts and web research
**Documents** (Setup → Documents & receipts; upload in Planner → Documents, the app's Planner → Docs, or drop files into `config/data/documents`):
- PDF, Word (.docx), text, Markdown and HTML are split into passages and made searchable by a **local** embedding model (`nomic-embed-text`). New files dropped into the folder are picked up every 10 minutes.
- Ask in the Documents card, or say `ask my documents when does my car insurance renew`. Your local model answers from the passages it finds and cites them ("[1] car-insurance.pdf, page 2"). If the answer isn't there, it says so.
- **Private.** Documents are read only by your local models, even when Claude is the AI brain: Claude only gets the local model's answer, never the documents.
- Scanned PDFs without a text layer can't be read yet (they show "no text found").

**Receipts:** take a photo (app: Planner → Docs → **Receipt photo**, or dashboard: **Receipt photo**). Your local vision model (`qwen2.5vl:7b`) reads the shop, date, total, items and any warranty period. The receipt becomes searchable ("when did I buy the headphones?"), and **Warranty** puts the end date on the calendar with a reminder a month before. Models: `ollama pull nomic-embed-text` and `ollama pull qwen2.5vl:7b` on the Ollama machine.

**Web research** (Setup → Web search): start the bundled private search engine with `docker compose --profile search up -d` (SearXNG, no account, no tracking, not exposed to your network). With the AI on, ask things like "find the best 2.5GbE switch under $100". Jarvis searches, reads the top pages and sums up with links. Without the AI, `search the web for ...` lists the results.
- Web pages are treated as untrusted text: the AI uses them as information and never follows instructions in them.
- The page reader only fetches public internet addresses. Anything on your network (10.x, 192.168.x, the Jarvis host) is refused, even through redirects.

## Location history (kept on your own server)
Your phone sends its location to Jarvis, and only to Jarvis:
- **OwnTracks** (free, open source, iPhone and Android): dashboard → **Places** → *Add phone (OwnTracks)* → scan the QR code with the phone. OwnTracks sets itself up. Each phone has its own code, and **Remove** locks it out (optionally deleting its history).
- **The Jarvis Android app:** Settings → **Share my location** (every 15 minutes; choose "Allow all the time" so it works while the app is closed).

What you get:
- **The Places page:** a map of any day (OpenStreetMap; your browser downloads the map pictures, your history stays on the server), your stops with times, distance travelled, and time at home, work and your other places.
- **Places:** Jarvis suggests Home and Work after a few days. Name any stop ("Gym", "Mum's") from the stops list, or say `call this place Gym`.
- **Chat:** `where am I`, `where was I yesterday at 3pm`, `how far did I travel this week`, `location yesterday`. With the AI on, also things like "how long was I at work on Monday?"
- **Location reminders:** `remind me to take the bins out when I get home`, `remind me to buy milk when I leave work`. They arrive through your reminder channels.
- **Privacy:**
  - Place names are never looked up online.
  - History older than a year is deleted every night (Setup → Location history to change that).
  - **Delete history...** on the Places page removes recent days or everything.
- **Away from home:** the phone needs your VPN to reach Jarvis. OwnTracks keeps points and sends them when it can.

## Email screening
Jarvis checks your inboxes every 15 minutes, and your **local AI** sorts each new message: important, personal, bill, newsletter, promotion, notification or spam. It also writes a one-line summary and picks out bills (payee, amount, due date).

- **Read-only.** The mailbox is opened read-only and messages are read without marking them read. Nothing is moved, deleted or flagged. Nothing is sent unless you switch sending on (see *Writing emails* below).
- **Local AI only.** Only your Ollama model reads email, even if Claude is your main AI brain. With no local model, simple rules sort mail (no summaries). Setup → Email screening AI picks the model; blank uses the AI brain's Ollama settings. `gpt-oss:20b` does it well.
- **Nothing kept but the summary.** Jarvis stores sender, subject, date, category and summary, never the message itself.
- **Email can't give orders.** It's treated as untrusted text: the screening model has no tools and must answer in a fixed format. A message that says "ignore your instructions" just gets filed (in testing, as spam).
- **Bills go in only with your OK.** Found bills appear in Planner → Email with a due date you can change, **Add** and **×**. Or say `add bill from email 12`.
- **Important mail** is announced through your reminder channels (Setup → Reminders & daily report). The daily report has an email section.
- **Chat:** `check my email`, `important emails`. With the AI on, also "anything from the bank this week?"

**Packages.** Order, shipping and delivery emails become a package list (Planner → Email, or `packages` / `where's my package`). You hear when something is **out for delivery**, **delivered** or **delayed**, and the daily report says what's arriving today. An older email never moves a package backwards.

**Subscription audit.** Receipts and renewal notices build a list of what you pay for: `subscriptions` shows each one with its amount, cycle and renewal date, plus a monthly and yearly total per currency.
- **Price rises** are announced ("Netflix is going up from 499 to 649 INR").
- **Yearly renewals** get a heads-up a week before.
- Jarvis can't see whether you actually use a service. Cancel on the service's own site, then press **Cancelled** (or say `cancelled Netflix`) to drop it from the list.

**Writing emails (off until you turn it on).** Setup → Email accounts → **Allow Jarvis to send** for one account. Then:
- `reply to Priya: yes, 8pm works` or `email raj@example.com saying I'll be late`. With the AI on, just say what you mean ("tell Priya yes to dinner and ask if I should bring dessert") and it writes the email.
- Names are looked up from mail you've received. A message to someone who wrote recently goes into their thread, with the subject `Re: ...`.
- **You always see the exact email (from, to, subject, text) and approve it with a tap, in every mode, Auto included.** Read-only mode blocks sending.
- The approval card warns you when the email answers spam or a first-time sender. Emails sometimes try to trick assistants into sending things.
- Every sent email is written to the audit log.
- Outgoing servers are filled in for the usual providers. Proton: the Bridge machine, port 1025. Outlook: add the `SMTP.Send` permission (step 3 below) and click **Sign in with Microsoft** again after turning sending on.

**Adding accounts** (Setup → Email accounts, add as many as you like):

| Provider | What to enter |
|---|---|
| Gmail | Turn on 2-Step Verification, then create an app password at myaccount.google.com/apppasswords and paste it. Your normal password won't work |
| iCloud | An app-specific password from appleid.apple.com → Sign-In and Security |
| Yahoo, AOL | Account security → Generate app password |
| Fastmail, Zoho | An app password from the account's security settings |
| Proton | Proton has no IMAP of its own: run **Proton Mail Bridge** (the official app, or a Bridge Docker container on the same machine as Jarvis). Use the username and password Bridge shows for the address. Server = the Bridge machine, port 1143, security STARTTLS, **Check certificate** off (Bridge uses its own certificate) |
| Outlook / Hotmail / Microsoft 365 | Microsoft blocks passwords for mail apps, so Jarvis signs in the Microsoft way. See below |
| Anything else | Provider **other**, plus the IMAP server, port and security your provider lists |

**Outlook, one-time setup (about 5 minutes):**
1. Go to entra.microsoft.com (or portal.azure.com) → App registrations → **New registration**. Name it "Jarvis mail". Supported account types: *Accounts in any organizational directory and personal Microsoft accounts*. No redirect URI. Register.
2. **Authentication** → Advanced settings → *Allow public client flows* → **Yes** → Save.
3. **API permissions** → Add a permission → *APIs my organization uses* → **Office 365 Exchange Online** → Delegated → `IMAP.AccessAsUser.All` (and `SMTP.Send` if you want Jarvis to send) → Add. (Work accounts may need an admin to grant consent, and IMAP must be allowed for the mailbox.)
4. Copy the **Application (client) ID**. In Jarvis: Setup → Email accounts → provider **outlook** → your address and that client ID → Save.
5. Click **Sign in with Microsoft**. Jarvis shows a code; open microsoft.com/devicelogin, enter it and approve. Jarvis keeps only the sign-in token (in `.env`) and renews it by itself.

## Talking to Jarvis (voice)
**Android app:** tap the mic and speak. Your words appear as you talk, and the answer is read aloud (Settings → Speak replies). The headset button turns on **hands-free**: after each answer Jarvis listens again, so you can keep talking without touching the phone. Say "stop", "that's all" or "thanks" to end it. If nothing is said for a few seconds, it stops listening until you tap the mic.

**Web dashboard:** the same mic and headset buttons in Chat. Replies are spoken by your browser's built-in voice. Listening needs Chrome or Edge, and browsers only allow the microphone on HTTPS or on the computer running Jarvis (`localhost`). Over plain `http://` on your network the buttons are greyed out; use HTTPS (for example `tailscale serve`) or the Android app. In Chrome, speech-to-text is done by Google's servers; the Android app uses your phone's speech service (often on-device).

**Approvals are never by voice.** When Jarvis needs a yes (anything destructive, or every change in Ask mode), it says "I need your OK on the screen" and waits for a tap. Someone else talking, or a TV, can't approve a reboot.

### "Hey Jarvis" (offline voice)
Speech is handled on your own network by a **Speaches** server: Whisper turns speech into text and Kokoro speaks the answers. Nothing goes to the cloud.

1. **Start the voice server** next to Jarvis (CPU):
   ```
   docker compose --profile voice up -d
   ```
   Or run Speaches anywhere else, for example `ghcr.io/speaches-ai/speaches:latest-cuda` on a machine with a GPU (it's also in Unraid's Community Apps).
2. **Setup → Voice (Hey Jarvis):** keep `http://voice:8000` for the bundled one, or enter the other server's address. **Save & test** downloads the speech and voice models the first time (a few hundred MB), then checks a full round trip. The defaults are picked for a CPU: a small, fast English Whisper model and the British `bm_george` voice. Other languages and bigger models are explained under each field.
3. **Setup → Listening devices → Add device** (for example "Kitchen Pi"). You get an access code, shown once. Each device has its own code; removing the device locks it out. Device codes only work for voice requests, never for the dashboard.
4. **Install the listener** on the device with the microphone and speaker:
   - **Raspberry Pi / Debian / Ubuntu:**
     ```
     curl -fsSL https://raw.githubusercontent.com/gtzapper/jarvis/main/listener/install-pi.sh -o install-pi.sh
     sh install-pi.sh
     ```
     It asks for Jarvis's address and the access code, then starts on boot. `journalctl -u jarvis-listener -f` shows what it hears.
   - **Windows** (Python 3.10+ from python.org): download `listener/install-windows.ps1` and run `powershell -ExecutionPolicy Bypass -File install-windows.ps1`. It starts when you sign in.
   - Pick another mic or speaker: `python jarvis_listener.py devices`, then set `input_device` / `output_device` in the config file it prints.

   Say "Hey Jarvis", wait for the chime, and talk. After an answer it listens about 5 seconds for a follow-up without the wake word. The wake word is detected on the device itself; nothing is sent until you say it. It also speaks reminders and the daily report (Setup → Reminders & daily report).

When a spoken request needs your OK, the device says "That needs your OK" and the question appears in the dashboard chat and the Android app, labelled with the device's name. With ntfy set up, your phone also gets a push alert. Once you tap, the device speaks the result.

## Permission modes (Ask, Auto, Read-only)
Like Claude Code, Jarvis has modes for how much it may do without asking. Switch them with the pill above the chat box, or type `ask mode`, `auto mode` or `read-only mode` (`mode` shows the current one).

| Mode | Looking (status, `df -h`, `docker ps`, logs) | Everyday changes (restart a container, add a movie, install updates, `systemctl restart`) | Destructive (reboot, delete, Docker cleanup, remove a download, `rm`, `docker rm`, `systemctl stop`) |
|---|---|---|---|
| **Auto** (default) | just runs | just runs, and says it did | **always asks** |
| **Ask** | asks | asks | **always asks** |
| **Read-only** | just runs | blocked; it tells you what it would do | blocked |

Destructive actions ask in every mode, and the prompt is marked "Destructive, so I always ask". Shell commands are sorted automatically: known read-only commands count as looking; anything that deletes, overwrites (`>` to a file, `sed -i`, `mv`), stops, kills, wipes or pipes into a shell counts as destructive; everything else is an everyday change. Auto-approved and blocked actions are written to `data/audit.log` too. The mode is saved in `data/mode.json`, so it survives restarts.

## Offline AI (local model with Ollama)
The same brain and the same tools can run on a local model instead of Claude. Nothing leaves your network, it works with the internet down, and there's no per-use cost. The trade-off: local models are less capable. Expect them to handle everyday requests well ("what's downloading", "who's watching", "pause the torrents"), but they make more mistakes on multi-step jobs.

**1. Run Ollama somewhere with a GPU.** Pick one:
- **Unraid:** Apps → search "Ollama" (image `ollama/ollama`). Add your GPU (with the Nvidia Driver plugin, add `--runtime=nvidia` and `NVIDIA_VISIBLE_DEVICES=all`), and map port 11434.
- **A Proxmox VM or LXC with GPU passthrough** (Linux): `curl -fsSL https://ollama.com/install.sh | sh`
- **A Windows PC with a GPU:** install from ollama.com.

To reach it from other machines, set `OLLAMA_HOST=0.0.0.0` on the Ollama machine. Only expose port 11434 on your LAN, never to the internet; Ollama has no login.

**2. Download a model that supports tool calling**, on the Ollama machine:
```
ollama pull qwen3:8b
```
Rough guide by GPU memory:

| GPU memory | Model | Notes |
|---|---|---|
| 8 GB | `qwen3:8b` | Good default |
| 16 GB | `qwen3:14b` or `gpt-oss:20b` | Noticeably better at multi-step jobs |
| 24 GB+ | `qwen3:30b` | Closest to the cloud |

**3. In Setup → AI brain:**
1. Set Provider to `ollama`.
2. Enter the Ollama address (for example `http://192.168.1.20:11434`) and the model.
3. Click **Save & test**.

The test shows whether Ollama is reachable, whether the model is installed and whether it supports tools, and reports its speed in tokens per second.

**How it stays safe with a smaller model:**
- **Confirmations still apply.** Every change goes through the same Yes/No and audit log as before.
- **Arguments are checked first.** Before anything runs, Jarvis checks the model's tool arguments: it corrects safe type mix-ups, and sends anything else (an unknown tool, an invalid value) back to the model as an error.

**Context size:** Ollama's default context window is only 4k tokens on GPUs under 24 GB, which is too small for Jarvis's tool list. Jarvis asks for 32k by default. If the model runs out of GPU memory, lower "Context size" in Setup, but don't go below about 12k.

Switching between Claude and Ollama is just the Provider setting. Your API key stays saved either way.

## Finding your media apps automatically
Setup → **Find media apps automatically** → Scan (or say `find media apps` in the chat). Jarvis looks for Plex, Sonarr, Radarr, Lidarr, Prowlarr, Overseerr/Jellyseerr, qBittorrent, Deluge and SABnzbd in three places:

- **Docker on Unraid and on your VMs/LXCs** (any machine under Unraid or VMs / servers): it lists the containers, recognises the apps by image or name (linuxserver, hotio, binhex, plexinc and others), and works out the address from the published ports. Host networking and containers with their own IP (Unraid br0/macvlan) work too.
- **Apps installed directly in Proxmox VMs and LXCs**: it checks each running guest's IP on the apps' usual ports.
- Every find is confirmed by asking the app to identify itself, so nothing is guessed.

Where Jarvis can log into the machine, it reads the API key from the app's own config file (Sonarr/Radarr/Lidarr/Prowlarr `config.xml`, Plex `Preferences.xml`, Overseerr `settings.json`, `sabnzbd.ini`). Then **Add** saves and tests it in one click. Keys stay on the Jarvis server and go into `.env`; the browser only ever sees "key found". qBittorrent and Deluge store hashed passwords, so you type those (or leave qBittorrent's blank if you whitelisted Jarvis's IP).

Reading keys needs root inside the container or guest: log in as root, or give the Jarvis user passwordless sudo (or use password login). Scanning changes nothing.

## Windows VMs and PCs
Windows machines are monitored and controlled over the same SSH key, using Windows' built-in **OpenSSH Server** and **PowerShell**.

**What Jarvis checks:**
- CPU, memory, uptime and every drive's free space.
- **Automatic services that crashed.** Services that stop themselves normally, like the Maps and Edge updaters, are ignored. Add your own under "Windows services to ignore".
- **Waiting Windows updates**, checked every 6 hours because the search is slow.
- **A pending restart** after updates.
- Docker Desktop containers, if you use it.

`run ... on gaming-pc` takes **PowerShell** commands, for example `run Get-PSDrive C on gaming-pc`. You confirm each one as usual, and destructive ones (`Restart-Computer`, `Remove-Item -Recurse`, `Stop-Service`…) get an extra warning.

**Setup**, once per Windows machine. Setup → Discovered on Proxmox shows this snippet with Jarvis's key already filled in. Run it in PowerShell **as Administrator**:
```
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic; Start-Service sshd
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' -Protocol TCP -LocalPort 22 -Action Allow -ErrorAction SilentlyContinue
Add-Content -Path "$env:ProgramData\ssh\administrators_authorized_keys" -Value '<Jarvis public key>'
icacls "$env:ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```
Then click **Add** in the discovered list. For a physical PC, add it under Setup → VMs / servers with Operating system = `windows`. The SSH user is an administrator account on that machine.

## Housekeeping: updates, restarts, cleanup
Tell Jarvis to look after your machines, in chat or in plain English with the AI ("update the docker vm", "anything to update?", "restart sonarr"):

| Say | What happens |
|---|---|
| `check updates on docker-vm` (or `on all`) | Lists waiting OS updates and containers with newer images. Image checks download the new images, but nothing that's running is touched |
| `update docker-vm` | Installs OS updates **and** updates containers on that machine |
| `update packages on X` / `update containers on X` | Just one of the two |
| `update plex container on docker-vm` | Specific containers (comma-separate several) |
| `update everything` | Every machine Jarvis may run commands on, in one go |
| `restart sonarr on docker-vm` | Restart one container |
| `clean up docker on X` | Remove unused images and build cache (containers and volumes are left alone) |
| `reboot docker-vm` | Restart the machine |

**Before anything changes, Jarvis shows you the exact list** (for example "nas: 3 OS updates (openssl, curl, libc6) and new images for 2 containers (sonarr, plex)") and waits for your yes. If an update needs a reboot, it asks separately. Everything goes to the Activity log.

**How it does it:**
- **OS updates:** it detects the package manager (apt, dnf, yum, apk, pacman, zypper). On Proxmox hosts it uses `dist-upgrade`, as Proxmox recommends. Config files you've changed are kept. If automatic updates are already running, it says so instead of fighting over the lock.
- **Containers started with Docker Compose** are pulled and recreated with `docker compose up -d` for just those services. **Other containers** (plain `docker run`, or Unraid's Docker tab) get the new image downloaded, and Jarvis tells you to recreate them the way you created them. On Unraid that's "apply update" in the Docker tab. Rebuilding someone else's `docker run` command from memory isn't safe to guess.
- **Root access:**
  - Logging in as root works directly.
  - Other users need **passwordless sudo**, or **password login** (Jarvis then answers sudo with that password).
  - For containers, a user in the `docker` group is enough.

**Not covered yet:** installing Windows updates (waiting updates are shown; install them with Windows Update), and Unraid OS updates (Tools → Update OS in Unraid).

## Running commands on your VMs
Any host under `linux_hosts` can take commands. Unraid hosts can too if you set `allow_commands: true` on them, since they run as root.
```
you> run df -h on docker-vm
you> on web1: sudo apt update && sudo apt upgrade -y
you> hosts
```
In Ask mode Jarvis shows you the exact command and host and waits for a yes. In Auto mode, read-only and everyday commands just run, and you see each one in the chat. Commands that look destructive (`rm`, `reboot`, `mkfs`, `docker rm`, `systemctl stop` and similar) always ask, with a warning. Output is trimmed to the last 40 lines. Every command, including the ones you decline, is written to `data/audit.log`.

## Creating VMs (with a static IP on pfSense)
```
you> check vm setup                                 # tests everything, changes nothing
you> create vm web1 with 4 cores 8gb ram 64gb disk
```
Jarvis does the following:
1. Clones your cloud-init template.
2. Sets CPU, RAM, disk, your SSH key and a `jarvis` user with sudo.
3. Picks a free IP from `ip_pool`. It skips anything already reserved, seen on the network, or inside pfSense's dynamic range.
4. Creates a DHCP static mapping on pfSense for the new VM's MAC address.
5. Starts the VM and waits for SSH.
6. Adds the VM to `data/hosts.yaml`, so it's monitored and accepts `run ... on web1` right away.

It refuses names that are already taken, and sizes over your `limits`. If the pfSense step fails, the VM is left **stopped** and Jarvis tells you exactly what to fix. It never deletes anything.

### One-time setup
**1. A cloud-init template** (Proxmox shell, Ubuntu 24.04 shown):
```
wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
qm create 9000 --name ubuntu-2404-template --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci
qm set 9000 --scsi0 local-lvm:0,import-from=/root/noble-server-cloudimg-amd64.img
qm set 9000 --ide2 local-lvm:cloudinit --boot order=scsi0 --serial0 socket --vga serial0 --agent enabled=1
qm template 9000
```

**2. A write-capable Proxmox token**, separate from the read-only monitoring one:
```
pveum role add JarvisProvision -privs "VM.Allocate VM.Clone VM.Audit VM.PowerMgmt VM.Config.CDROM VM.Config.CPU VM.Config.Cloudinit VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options Datastore.AllocateSpace Datastore.Audit SDN.Use Sys.Audit"
pveum user add jarvis-admin@pve
pveum aclmod / -user jarvis-admin@pve -role JarvisProvision
pveum user token add jarvis-admin@pve provision --privsep 0
```
Put the token ID and secret in `.env` as `PVE_ADMIN_TOKEN_ID` and `PVE_ADMIN_TOKEN_SECRET`. On Proxmox 7, drop `SDN.Use` from the list.

**3. A pfSense user that may write DHCP mappings.** Create a second user (e.g. `jarvis-admin`), separate from the read-only `jarvis` one. Give it these privileges:
- **POST:** `/api/v2/services/dhcp_server/static_mapping`, `/api/v2/services/dhcp_server/apply`
- **GET:** `/api/v2/services/dhcp_servers`, `/api/v2/services/dhcp_server/static_mappings`, `/api/v2/diagnostics/arp_table`

Do **not** give it "Deny Config Write". Create a key as that user and put it in `.env` as `PFSENSE_ADMIN_API_KEY`.

**4.** Fill in the `provisioning:` section of `config.yaml`. Make sure `ip_pool` sits **outside** pfSense's dynamic DHCP range (Services → DHCP Server → LAN).

Then run `check vm setup`. On pfSense 2.8 with the Kea DHCP backend, do one test VM first and check that the mapping appears under Services → DHCP Server → LAN → Static Mappings. The REST API package's Kea support is newer than its ISC support.

## Jarvis on your phone (installable app)
The dashboard works as a phone app: full screen, its own icon, a bottom tab bar, and long-press shortcuts to Chat and Lab status.

1. On your phone, open Jarvis in Chrome (for example `http://10.0.20.x:8080`) and log in.
2. Chrome menu (⋮) → **Add to Home screen** (or **Install app**).

The dashboard has to listen on your network for this, so set a dashboard password and start it with host `0.0.0.0`.

**HTTP vs HTTPS:** Chrome only fully installs a web app from a secure address. Over plain `http://` on your Wi-Fi you still get a home-screen icon, but it may open as a normal Chrome tab with the address bar. To get the full app, reach Jarvis over HTTPS. The easiest way is Tailscale: install it on the Jarvis host and your phone, turn on MagicDNS and HTTPS certificates in the Tailscale admin console (DNS page), then run `tailscale serve --bg 8080` on the host. That gives a private `https://<name>.<tailnet>.ts.net` address that only your devices can reach. Don't open Jarvis to the internet directly.

There's also a native Android app (below), which doesn't need HTTPS on your home network.

## Android app
A native app in `android/`: chat with live steps, approve or decline actions (destructive ones in red), the Ask / Auto / Read-only switch, voice input with spoken replies, lab status, the activity log, and lab alerts as phone notifications. Alerts work by the phone checking your Jarvis every 15 minutes; no Google push service or outside server is involved.

**Install:** copy `Jarvis.apk` to your phone and open it. Android asks you to allow installs from that app (Files, Chrome...) the first time. Then enter your Jarvis address (the one you open the dashboard with) and the dashboard password. The password isn't stored on the phone; the app keeps the login session, like a browser.

**Themes:** Settings → Theme: System, Light, Dark, Midnight, Ember, Forest or High contrast (the same set as the dashboard's Setup → Appearance).

**Updates:** the app looks for a new version once a day (Settings → App updates, or tap **Check now**). When there is one, it shows what changed, downloads it inside the app, checks it, and opens Android's installer. The first time, Android asks you to allow Jarvis to install apps. "Skip this version" stops the daily reminder for that version.

The app finds new versions on this repo's GitHub Releases: a release tagged `android-v<version>` (for example `android-v1.2`) with the `.apk` attached.

**Publishing a new version:**
1. In `android/app/build.gradle.kts`, raise `versionCode` by one and set `versionName` (for example `1.2`).
2. Build the signed APK. `JARVIS_KEYSTORE_PROPS` points at your signing key's settings file:
   ```
   cd android
   set JARVIS_KEYSTORE_PROPS=D:\AndroidBuild\keys\keystore.properties
   gradlew assembleRelease
   ```
   The APK appears at `android/app/build/outputs/apk/release/app-release.apk`.
3. On GitHub: Releases → Draft a new release → tag `android-v1.2` → write what changed (the app shows this text) → attach the APK → Publish.

**Keep the signing key safe.** Android only installs an update if it's signed with the same key as the installed app. The key is `jarvis-release.jks` plus `keystore.properties` (which holds its password), kept outside the repo. Back both up somewhere safe. If they're lost, every phone has to uninstall the app and install it fresh. Never commit them.

**Build it yourself:** open `android/` in Android Studio, or run `gradlew assembleDebug` inside `android/` (needs the Android SDK, and Java 17 or newer). A debug build is signed with your own debug key, so it can't update over a release build, or the other way round.

The app allows plain `http://` because Jarvis normally runs at a local address. Away from home, connect through a VPN (WireGuard on pfSense, or Tailscale) rather than opening Jarvis to the internet.

## Phone alerts (optional)
Install the **ntfy** app, subscribe to a long random topic name, and set `NTFY_URL=https://ntfy.sh/<that-topic>` in `.env`. Anyone who knows the topic name can read it, so make it unguessable, or self-host ntfy.

## Roadmap
- **Apple Health** (planned): steps, sleep, heart rate and workouts from the iPhone (Health Auto Export or an Apple Shortcut), with trends and a line in the daily report.
1. ✅ Monitoring core
2. Run 24/7 (Docker container or LXC on Proxmox)
3. ✅ AI brain: free-form English on top of the text commands. Ask "how's the NAS?" in plain English (Claude API or local Ollama)
4. Voice: wake word, Whisper, Piper
5. ✅ Actions with confirmation: run commands, create VMs with static IPs
