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
4. **Install the listener** on a Raspberry Pi or a PC with a microphone and speaker (coming next).

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
1. ✅ Monitoring core
2. Run 24/7 (Docker container or LXC on Proxmox)
3. ✅ AI brain: free-form English on top of the text commands. Ask "how's the NAS?" in plain English (Claude API or local Ollama)
4. Voice: wake word, Whisper, Piper
5. ✅ Actions with confirmation: run commands, create VMs with static IPs
