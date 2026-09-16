# Crafty + Azure Discord Control Panel

A Discord bot that turns a Discord server into a control panel for a Minecraft
server managed by a **remote [Crafty Controller 4](https://craftycontrol.com)**
instance, including the lifecycle of the **Azure virtual machine** that hosts it.

The bot itself is designed to run 24/7 on a **Raspberry Pi Zero W**: it is a
lightweight async API client with no database, no web server and no polling loops.

Forked from [Two-Play/Crafty-Discord-bot](https://github.com/Two-Play/Crafty-Discord-bot)
and rebuilt around modern slash commands, a service layer and Azure orchestration.

---

## Contents

1. [Architecture](#architecture)
2. [Features](#features)
3. [Command reference](#command-reference)
4. [Requirements](#requirements)
5. [Installation on a Raspberry Pi Zero W](#installation-on-a-raspberry-pi-zero-w)
6. [Updating](#updating)
7. [Discord setup](#discord-setup)
8. [Crafty setup](#crafty-setup)
9. [Azure setup](#azure-setup)
10. [Environment variables](#environment-variables)
11. [How the orchestration works](#how-the-orchestration-works)
12. [Permissions model](#permissions-model)
13. [Security considerations](#security-considerations)
14. [Resource usage and caching](#resource-usage-and-caching)
15. [Crafty API coverage](#crafty-api-coverage)
16. [Development and tests](#development-and-tests)
17. [Migrating from the original bot](#migrating-from-the-original-bot)
18. [Troubleshooting](#troubleshooting)
19. [Credits and licence](#credits-and-licence)

---

## Architecture

Crafty is **remote**. Nothing Minecraft-related runs on the Pi; it only talks to
two HTTPS APIs.

```
Raspberry Pi Zero W                    Azure VM (e.g. West Europe)
┌────────────────────────┐             ┌────────────────────────────────┐
│  Discord bot           │             │  Crafty Controller 4.x         │
│                        │  HTTPS      │    └── Minecraft server (Paper)│
│  ├── CraftyService ────┼────────────▶│      REST API :8443            │
│  ├── AzureService  ────┼───┐         └────────────────────────────────┘
│  └── Orchestrator      │   │  HTTPS (management.azure.com)
└────────────────────────┘   └────────▶ Azure Resource Manager
            ▲                            (start / deallocate / status)
            │ WebSocket (Discord gateway)
     Discord users
```

Layering (each arrow is the only way to cross the boundary):

```
Discord slash command / button
        ↓
  cog (bot/cogs/…)          – validates permissions, renders embeds
        ↓
  orchestrator              – sequences multi-step workflows
        ↓
  CraftyService / AzureService  – async HTTP, retries, typed errors
        ↓
  Crafty v2 API / Azure Resource Manager
```

Because every Crafty detail is confined to `bot/services/crafty.py`, a future
Crafty API change touches exactly one file.

```
bot/
├── __main__.py        entrypoint (python -m bot)
├── client.py          Discord client, startup validation, error surface
├── config.py          environment parsing and validation
├── permissions.py     three configurable permission tiers
├── cache.py           tiny TTL cache with single-flight requests
├── tasks.py           optional idle watcher (disabled by default)
├── errors.py          typed, user-safe exceptions
├── utils.py           logging (with secret scrubbing), formatting, backoff
├── services/
│   ├── crafty.py      Crafty Controller v2 API client
│   ├── azure.py       Azure ARM client + credential provider
│   └── orchestrator.py Azure ↔ Crafty workflows
├── ui/
│   ├── embeds.py      reusable embed builders
│   └── views.py       buttons, confirmations, select menus
└── cogs/              status, server, azure, minecraft, schedule

deploy/
├── crafty-bot.service systemd unit
└── update.sh          manual updater (pull, reinstall, restart)
```

---

## Features

* **`/status`** — one embed with the Minecraft state, player count, version,
  uptime, CPU/RAM/disk and the Azure VM power state, with
  `🔄 Refresh` / `▶ Start` / `⏹ Stop` / `🔃 Restart` buttons that edit the same
  message instead of spamming the channel.
* **Graceful shutdown** — the VM is never deallocated while Minecraft is running
  unless an administrator explicitly forces it.
* **Inactivity auto-shutdown** — `/timeout 90`, or the **Auto-shutdown** switch
  under `/status`, stops the server gracefully through Crafty once it has been
  up and empty for 90 minutes, then frees the Azure VM. It stays armed across
  sessions, and its state is shown wherever it is relevant: `/status`,
  `/server status`, `/server players`, `/servers` and `/health`.
* **Live progress** — start/stop workflows update a single message step by step
  (`Azure VM → Crafty → Minecraft`) using exponential-backoff polling, never
  fixed sleeps.
* **Console access** — `/server command`, with a confirmation button for
  dangerous commands such as `stop` or `ban`.
* **Logs, players, backups, scheduler** — everything the Crafty v2 API actually
  supports, and nothing it does not.
* **Push notifications with zero polling** — `/webhook create` points Crafty's
  own webhook system at a Discord channel, so starts, stops, crashes, backups
  and jar updates are announced *by the Crafty host*. The Pi does no work at all,
  and the announcements keep arriving even while the bot is offline.
* **Multi-server overview** — `/servers` lists every server in a single
  `GET /servers/status` call, however many are configured.
* **Text-drawn charts** — `/server history` renders the last hour of CPU, RAM and
  player count as Unicode sparklines. No matplotlib, no image rendering, no
  megabytes of dependencies on a 512 MB Pi.
* **Read-only file access** — `/server properties` and `/server roster` show
  `server.properties`, the whitelist, the operator list and the ban list without
  opening the Crafty panel.
* **Crafty-side schedules** — `/schedule create` sets up a cron task that runs on
  the Crafty host, so a nightly restart or backup fires whether or not the bot is
  running.
* **Granular permissions** — read-only for everyone, Minecraft control for a
  role, Azure control for another, destructive actions for administrators.
* **VM-aware Crafty calls** — Crafty lives on the Azure VM, so while that VM is
  stopped or deallocated the bot skips the Crafty API entirely instead of waiting
  for TCP timeouts, and says why.
* **VM-only control** — `/azure start` brings up just the VM; Minecraft only
  starts when you ask for it with `start_minecraft:true`.
* **Fails soft** — if Crafty is unreachable the bot still starts, still answers,
  and tells you the VM's power state (which is usually the reason).

---

## Command reference

| Command | Tier | What it does |
| --- | --- | --- |
| `/status [server]` | everyone | Full infrastructure overview with action buttons |
| `/servers` | everyone | Every server Crafty publishes, in one API call |
| `/timeout [minutes] [shutdown_vm] [server]` | everyone to read; server to arm (+azure for `shutdown_vm`) | Stop the server gracefully after N minutes up-and-empty, then free the VM. Stays armed until `minutes:0` cancels it; no argument shows the state |
| `/health` | everyone | Which layer is broken: bot, Crafty, Azure or Minecraft (ephemeral) |
| `/server status [server]` | everyone | Detailed server statistics |
| `/server players [server]` | everyone | Online players, with UUIDs when Crafty reports them |
| `/server info [server]` | everyone | Server configuration (type, address, autostart, …) |
| `/server resources` | everyone | CPU/RAM/disk of the Crafty host (i.e. the VM) |
| `/server history [server]` | everyone | CPU, RAM and player sparklines for the last hour |
| `/server logs [lines] [source] [server]` | server | Last log lines from the console buffer or `latest.log` (ephemeral) |
| `/server properties [server]` | server | Read `server.properties` (ephemeral, read-only) |
| `/server roster which:<list> [server]` | server | Read the whitelist, operator list or ban list (ephemeral) |
| `/server start [server]` | server | Start Minecraft (starts the VM first if needed) |
| `/server stop [server] [shutdown_vm]` | server (+azure for `shutdown_vm`) | Graceful stop, optionally deallocating the VM |
| `/server restart [server]` | server | Restart through Crafty |
| `/server command command:<text> [server]` | server | Send a console command; dangerous ones ask for confirmation |
| `/server backup [server]` | server | Run a Crafty backup configuration |
| `/server backups [server]` | server | List backup configurations |
| `/server kill [server]` | admin | Force-kill a frozen server (asks for confirmation) |
| `/server update [server]` | admin | Install the jar update Crafty found (asks for confirmation) |
| `/azure status` | everyone | VM power state, region, size, public IP |
| `/azure ip` | everyone | Public and private IP addresses |
| `/azure start [start_minecraft] [server]` | azure | Start the VM only; `start_minecraft:true` also waits for Crafty and starts Minecraft |
| `/azure stop [force] [server]` | azure (`force`: admin) | Stop Minecraft, then deallocate the VM (asks for confirmation) |
| `/azure restart [server]` | admin | Stop Minecraft, then reboot the VM |
| `/minecraft start [server]` | server (+azure if the VM is down) | Bring the whole stack up |
| `/minecraft stop [shutdown_vm] [server]` | server (+azure for `shutdown_vm`) | Stop Minecraft, optionally the VM too |
| `/minecraft restart [server]` | server | Restart, starting the VM if necessary |
| `/schedule info task_id:<n> [server]` | server | Show one Crafty scheduled task |
| `/schedule run task_id:<n> [cascade] [server]` | server | Run a Crafty task now, optionally cascading its chain |
| `/schedule create action:<a> cron:<expr> [name] [command] [backup_id] [server]` | admin | Create a Crafty cron schedule |
| `/schedule toggle task_id:<n> enabled:<bool> [server]` | admin | Pause or resume a schedule |
| `/schedule delete task_id:<n> [server]` | admin | Delete a schedule (asks for confirmation) |
| `/webhook list [server]` | server | Crafty's event webhooks (URLs are never shown) |
| `/webhook create url:<url> [events] [name] [server]` | admin | Have Crafty announce events in a Discord channel |
| `/webhook test webhook_id:<id> [server]` | server | Fire a sample event through a webhook |
| `/webhook toggle webhook_id:<id> enabled:<bool> [server]` | admin | Enable or disable a webhook |
| `/webhook delete webhook_id:<id> [server]` | admin | Remove a webhook (asks for confirmation) |

Every command with a `server` option autocompletes the servers your Crafty API
key can see, so multiple Crafty servers work out of the box.

Examples:

```
/status
/server command command:say Hello from Discord!
/server logs lines:30 source:Server log file (latest.log)
/minecraft start
/minecraft stop shutdown_vm:true
/azure start
/azure start start_minecraft:true
/azure stop force:true
/schedule run task_id:4 cascade:true
```

---

## Requirements

| Component | Requirement |
| --- | --- |
| Bot host | Raspberry Pi Zero W (512 MB RAM) or anything larger |
| Python | **3.11+** (Raspberry Pi OS Bookworm ships 3.11) |
| Crafty | Crafty Controller **4.x** reachable over HTTP(S) — verified against 4.10.8 |
| Azure | A VM plus credentials that may read it and start/stop it (optional) |
| Discord | A bot application; **no privileged intents required** |

Runtime dependencies: `discord.py`, `aiohttp`, `python-dotenv` and, optionally,
`azure-identity`. No database, no Redis, no web framework.

---

## Installation on a Raspberry Pi Zero W

Tested on **Raspberry Pi OS Lite (Bookworm, 32-bit)**, which ships Python 3.11
and still supports ARMv6. Two things about the Pi Zero W matter before you start:

* It has 512 MB of RAM and one 1 GHz core, so give it swap for the install step:
  `sudo dphys-swapfile swapoff && sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile && sudo dphys-swapfile setup && sudo dphys-swapfile swapon`
* It has no real-time clock. HTTPS certificate validation and Azure tokens both
  fail if the clock is wrong, so make sure time sync is healthy:
  `timedatectl status` should say *System clock synchronized: yes*.

Because it is an ARMv6 device, a few packages have no prebuilt wheels.
Installing those from Debian's repository avoids a multi-hour compile.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git \
                    python3-aiohttp python3-cryptography

sudo useradd --system --create-home --home-dir /opt/crafty-bot craftybot
sudo -u craftybot git clone https://github.com/n-popescu/Crafty-Discord-bot.git /opt/crafty-bot
cd /opt/crafty-bot

# --system-site-packages reuses the apt-installed aiohttp/cryptography builds.
sudo -u craftybot python3 -m venv --system-site-packages .venv
sudo -u craftybot .venv/bin/pip install --upgrade pip
sudo -u craftybot .venv/bin/pip install -r requirements.txt
```

If `azure-identity` fails to build (it pulls in `cryptography`, which needs Rust
on ARMv6), install everything else and use service-principal credentials — see
[Azure authentication on a Pi Zero W](#azure-authentication-on-a-pi-zero-w):

```bash
sudo -u craftybot .venv/bin/pip install "discord.py>=2.4,<3" "aiohttp>=3.8,<4" "python-dotenv>=1.0,<2"
```

Configure and test:

```bash
sudo -u craftybot cp .env.example .env
sudo -u craftybot nano .env          # fill in the values
sudo chmod 600 .env                  # the file contains secrets
sudo -u craftybot .venv/bin/python -m bot   # Ctrl+C once the startup report looks right
```

A healthy start looks like this:

```
Starting Crafty Control Panel bot
Crafty URL: https://crafty.example.com (TLS verification: True)
Azure VM: mc-vm (resource group mc-rg)
Registered 6 slash commands in guild 123456789012345678
Startup status:
  Discord: 🟢 connected as CraftyPanel#1234
  Crafty:  🟢 connected (https://crafty.example.com)
  Azure:   🟢 authenticated (VM mc-vm)
```

Install the service:

```bash
sudo cp deploy/crafty-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crafty-bot
journalctl -u crafty-bot -f
```

The unit file restarts the bot on failure, caps memory at 200 MB and reads the
environment from `/opt/crafty-bot/.env`.

> **Docker** is available (`docker build -t crafty-bot .`) but is not recommended
> on a Pi Zero W: the container runtime costs more memory than the bot itself.

---

## Updating

`deploy/update.sh` is a manual updater: it fast-forwards the checkout, reinstalls
dependencies only when `requirements.txt` changed, restarts the service and
prints its status.

```bash
sudo /opt/crafty-bot/deploy/update.sh
```

```
==> Fetching origin/main
==> Updating 4105a18 -> 9f3c1d2
9f3c1d2 Skip Crafty calls while the Azure VM is powered off
==> Installing dependencies into /opt/crafty-bot/.venv
==> Restarting crafty-bot
==> Now running 9f3c1d2 on main.
```

It refuses to run if the checkout has local modifications (`.env` is ignored, so
your configuration is never touched), and it never lets root write into the
checkout: git runs as the user that owns the directory, `systemctl` as root.

| Variable | Default | Purpose |
| --- | --- | --- |
| `REPO_DIR` | the script's own repository | Checkout to update |
| `BRANCH` | the checked-out branch | Branch to fast-forward to |
| `REMOTE` | `origin` | Remote to fetch from |
| `SERVICE` | `crafty-bot` | systemd unit to restart |
| `VENV` | `$REPO_DIR/.venv` | Virtualenv holding the dependencies |
| `FORCE` | `0` | `1` reinstalls and restarts even when already up to date |

```bash
# Track a different branch, or force a restart without any new commits.
sudo BRANCH=develop /opt/crafty-bot/deploy/update.sh
sudo FORCE=1 /opt/crafty-bot/deploy/update.sh
```

Docker deployments update the usual way instead:

```bash
docker pull ghcr.io/n-popescu/crafty-discord-bot:latest
docker compose up -d          # or: docker restart crafty-bot
```

---

## Discord setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application**.
2. **Bot** → **Reset Token** → copy the token into `DISCORD_TOKEN`.
3. Leave **all privileged intents off**. The bot uses slash commands only and
   never reads message content.
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`; bot
   permissions only need **Send Messages** and **Embed Links** (the bot replies
   to interactions, so even those are mostly a convenience). Invite the bot with
   the generated URL.
5. Enable **Developer Mode** in Discord (*Settings → Advanced*) so you can
   right-click to *Copy ID* for the server, roles and users.
6. Put your server's ID in `DISCORD_GUILD_ID`. Commands then appear instantly and
   the bot rejects interactions coming from anywhere else. Leaving it empty
   publishes the commands globally, which can take up to an hour to propagate.

Slash commands are registered automatically on every start, so there is no
`>sync` command to run.

---

## Crafty setup

### 1. Create an API key

In the Crafty panel: **Panel → Users → your user → API Keys → Create new API
Token**. Give the key only the permissions the bot needs:

| Crafty permission | Needed for |
| --- | --- |
| `COMMANDS` | `/server start`, `stop`, `restart`, `kill`, `/server command`, backups trigger |
| `TERMINAL` | `/server logs` (console buffer) |
| `LOGS` | `/server logs source:Server log file` |
| `BACKUP` | `/server backup`, `/server backups` |
| `SCHEDULE` | `/schedule info`, `/schedule run` |
| `PLAYERS` | reserved for future player commands |

Leave **Full Access** off, and prefer a dedicated Crafty user that can only see
the servers the bot should manage. Copy the token into `CRAFTY_API_TOKEN`; it is
shown once.

`/server resources` reads `GET /api/v2/crafty/stats`, which is available to any
authenticated user.

### 2. Find the server ID

Open the server in the Crafty panel and copy the UUID from the URL
(`/panel/server_detail?id=<uuid>`), or simply start the bot and use the
autocomplete on any `server` option. Set `CRAFTY_SERVER_ID` to make one server
the default; with a single visible server you can leave it empty.

### 3. Make Crafty reachable

The bot needs HTTPS access to Crafty's API port (`8443` by default).

* **Recommended:** put Crafty behind a reverse proxy with a trusted certificate
  (`CRAFTY_URL=https://crafty.example.com`, `CRAFTY_VERIFY_SSL=true`). See the
  [Crafty reverse-proxy guide](https://docs.craftycontrol.com/pages/getting-started/proxies/).
* **Direct exposure:** open `8443` in the Azure network security group,
  restricted to the Pi's public IP if it is static.
* **VPN / Tailscale (no public exposure):** install Tailscale on both the VM and
  the Pi and use the private address, e.g.
  `CRAFTY_URL=https://100.101.102.103:8443`. Crafty's own certificate is
  self-signed in that case, so set `CRAFTY_VERIFY_SSL=false`. The traffic is
  still encrypted by the VPN. This is the safest option if you would rather not
  expose Crafty at all.

---

## Azure setup

### 1. Gather the identifiers

```bash
az account show --query id -o tsv                 # AZURE_SUBSCRIPTION_ID
az vm list -o table                               # AZURE_RESOURCE_GROUP, AZURE_VM_NAME
```

### 2. Create a service principal with least privilege

Create a custom role limited to reading the VM and switching it on and off —
this is narrower than the built-in *Virtual Machine Contributor*, which can also
delete and reconfigure VMs.

`minecraft-vm-operator.json`:

```json
{
  "Name": "Minecraft VM Operator",
  "IsCustom": true,
  "Description": "Read a VM and start/stop/restart it. No create, delete or resize.",
  "Actions": [
    "Microsoft.Compute/virtualMachines/read",
    "Microsoft.Compute/virtualMachines/instanceView/read",
    "Microsoft.Compute/virtualMachines/start/action",
    "Microsoft.Compute/virtualMachines/deallocate/action",
    "Microsoft.Compute/virtualMachines/powerOff/action",
    "Microsoft.Compute/virtualMachines/restart/action",
    "Microsoft.Network/networkInterfaces/read",
    "Microsoft.Network/publicIPAddresses/read"
  ],
  "NotActions": [],
  "DataActions": [],
  "NotDataActions": [],
  "AssignableScopes": ["/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>"]
}
```

```bash
SUB=<SUBSCRIPTION_ID>
RG=<RESOURCE_GROUP>
VM=<VM_NAME>

az role definition create --role-definition @minecraft-vm-operator.json

# Scope the assignment to the single VM, not the whole resource group.
az ad sp create-for-rbac \
  --name "crafty-discord-bot" \
  --role "Minecraft VM Operator" \
  --scopes "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Compute/virtualMachines/$VM"
```

The output maps to the environment as follows:

| `az` output | Variable |
| --- | --- |
| `tenant` | `AZURE_TENANT_ID` |
| `appId` | `AZURE_CLIENT_ID` |
| `password` | `AZURE_CLIENT_SECRET` |

The network permissions are only needed for `/azure ip` and the public IP shown
in `/azure status`; drop them if you do not want them.

If you prefer a built-in role, **Virtual Machine Contributor** scoped to the
single VM also works — it is simply broader than necessary.

### 3. Azure authentication on a Pi Zero W

`AzureService` obtains tokens through
`azure.identity.aio.DefaultAzureCredential` when `azure-identity` is installed,
which supports environment variables, managed identity, the Azure CLI and
Workload Identity.

`azure-identity` depends on `cryptography`, which has no ARMv6 wheel. Two ways
around that:

* install `python3-cryptography` from apt and create the virtualenv with
  `--system-site-packages` (the [installation](#installation-on-a-raspberry-pi-zero-w)
  steps do this), or
* skip `azure-identity` entirely: with `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and
  `AZURE_CLIENT_SECRET` set, the bot performs the standard OAuth2
  client-credentials flow itself over `aiohttp`.

Either way the VM is driven through the Azure Resource Manager REST API rather
than the generated `azure-mgmt-compute` client, which keeps memory use low, and
the Azure CLI is never invoked as a subprocess.

---

## Environment variables

Copy `.env.example` to `.env`. Missing required values are reported by name at
startup — never by value.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | — | Discord bot token |
| `DISCORD_GUILD_ID` | | — | Register commands in (and accept them from) one guild |
| `CRAFTY_URL` | ✅ | — | Base URL of the remote Crafty instance |
| `CRAFTY_API_TOKEN` | ✅ | — | Crafty API key |
| `CRAFTY_VERIFY_SSL` | | `true` | Verify Crafty's TLS certificate |
| `CRAFTY_TIMEOUT` | | `10` | Per-request timeout in seconds |
| `CRAFTY_SERVER_ID` | | — | Default server for commands without `server` |
| `CRAFTY_UTC_OFFSET` | | — | UTC offset **of the Crafty host** in hours (`2`, `-5`, `5.5`). Set it when the Pi and the VM are in different time zones, or uptimes are off by the difference |
| `AZURE_SUBSCRIPTION_ID` | | — | Enables `/azure` when set with the next two |
| `AZURE_RESOURCE_GROUP` | | — | Resource group of the VM |
| `AZURE_VM_NAME` | | — | VM name |
| `AZURE_TENANT_ID` | | — | Service principal tenant |
| `AZURE_CLIENT_ID` | | — | Service principal application ID |
| `AZURE_CLIENT_SECRET` | | — | Service principal secret |
| `AZURE_TIMEOUT` | | `30` | Azure request timeout in seconds |
| `ADMIN_USER_IDS` | | — | Users who may do everything |
| `ADMIN_ROLE_IDS` | | — | Roles who may do everything |
| `SERVER_CONTROL_ROLE_IDS` | | — | Roles that may control Minecraft |
| `AZURE_CONTROL_ROLE_IDS` | | — | Roles that may control the VM |
| `AUTO_SHUTDOWN_VM` | | `false` | Deallocate the VM after Minecraft stops |
| `AUTO_SHUTDOWN_DELAY` | | `300` | Grace period before deallocating (seconds) |
| `IDLE_SHUTDOWN_ENABLED` | | `false` | Pre-arm `/timeout` for the default server at startup |
| `IDLE_SHUTDOWN_MINUTES` | | `30` | Idle minutes for that pre-armed timeout, and the default in the `/status` dialog |
| `TIMEOUT_CHECK_INTERVAL` | | `60` | Seconds between player-count checks while a timeout is armed (falls back to `IDLE_CHECK_INTERVAL`) |
| `TIMEOUT_STATE_FILE` | | `timeout_state.json` | Where armed timeouts are remembered across restarts; empty disables persistence |
| `STATUS_CACHE_TTL` | | `10` | Seconds a status reading may be reused |
| `START_TIMEOUT` | | `600` | Upper bound for start workflows (seconds) |
| `STOP_TIMEOUT` | | `300` | Upper bound for stop workflows (seconds) |
| `LOG_LEVEL` | | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

---

## How the orchestration works

### Crafty only exists while the VM runs

Crafty Controller runs *on* the Azure VM, so every Crafty request first checks the
cached VM power state (5 s TTL):

```
VM deallocated / stopped ──▶ no HTTP call at all
                             "The Azure VM that hosts Crafty is powered off."
VM running / starting / unknown ──▶ normal Crafty request
```

This applies to every command, to autocomplete and to the idle watcher, so a
powered-off VM costs no Crafty timeouts. Transitional states still send the
request — that is exactly what the start workflow polls for. If Azure itself
cannot be queried the gate opens, so an Azure outage never hides a healthy Crafty.

### Starting (`/minecraft start`, `/azure start start_minecraft:true`, `/status ▶`)

```
Is the VM running?  ──no──▶ POST …/start
                              ↓ poll instanceView (backoff 5s → 20s)
                            PowerState/running
                              ↓
                            poll GET /api/v2/crafty/check  (Crafty booting)
                              ↓
Is Minecraft running? ─no──▶ POST …/action/start_server
                              ↓ poll GET …/stats until running
                            ✅ ready
```

Each arrow updates the same Discord message. Nothing sleeps for a fixed period:
every wait is a poll with exponential backoff and a hard timeout
(`START_TIMEOUT`).

`/azure start` on its own stops after `PowerState/running`: the VM is up, Crafty
boots on it, and Minecraft stays down until someone runs `/minecraft start` (or
`/azure start start_minecraft:true`).

### Stopping (`/azure stop`)

```
VM already stopped? ──yes──▶ report and finish
        │no
        ▼
Minecraft running? ──yes──▶ POST …/action/stop_server
        │                     ↓ poll …/stats until running == false
        │                   (timeout ⇒ abort, VM stays up)
        ▼
POST …/deallocate  ──▶ poll until deallocated ──▶ ✅ billing stopped
```

Guarantees:

* The VM is **never** deallocated while Minecraft is running, unless an
  administrator passes `force:true`.
* If Crafty cannot confirm the shutdown, the workflow **aborts and leaves the VM
  running** rather than risking world corruption; the embed says so explicitly.
* Every destructive path is behind a confirmation button.

### Automatic shutdown

`AUTO_SHUTDOWN_VM=true` makes `/server stop` and `/minecraft stop` continue into
`wait until Minecraft stopped → wait AUTO_SHUTDOWN_DELAY → deallocate`. With the
default `false`, stopping Minecraft never touches the VM.

### Inactivity auto-shutdown (`/timeout`)

`/timeout minutes:90` arms a switch: once the server has had **nobody online**
for 90 minutes, the bot runs the ordinary stop workflow — a graceful Crafty
shutdown that saves the world and waits for the server to confirm it has
stopped — and only then deallocates the Azure VM.

```
/timeout minutes:90      arm: stop after 90 idle minutes, then free the VM
/timeout minutes:90 shutdown_vm:false   stop Minecraft only, leave the VM up
/timeout minutes:0       cancel
/timeout                 show the current state
```

The same switch sits under `/status` as an **Auto-shutdown: ON/OFF** button —
green when armed. Clicking it while off opens a small dialog asking for the
delay; clicking it while on cancels.

The countdown runs **only while the server is up and has zero players**:

| Server state | Countdown |
| --- | --- |
| Running, someone online | paused — any player at all counts as activity, however static the number is |
| Running, nobody online | **counting** |
| Stopped | paused — a stopped server is not idle, it is already stopped |
| VM powered off | paused — waiting for the server to come back |

It restarts from zero **every** time the server becomes empty again, so a
server that fills up and empties out gets a fresh full delay rather than
inheriting whatever had accrued earlier.

It is a **standing rule**, not a one-shot. Arming it once covers every session:
after it fires, it stays armed and simply waits for the server to come back up
and empty out again. Only `/timeout minutes:0` cancels it — nothing else does,
including the VM being deallocated by other means.

What it does and does not do:

* It **never kills** anything. Firing calls exactly the same
  `stop_minecraft` workflow as `/server stop`, so a timed shutdown and a manual
  one are the same shutdown, `AUTO_SHUTDOWN_DELAY` grace period included.
* **Accrued idle time is only trusted while the watcher was actually watching.**
  A failed check neither resets nor advances anything, but if the bot loses
  sight of the server for more than a few check intervals, the count starts
  again from the next reading. Otherwise a server that was busy during an
  outage and emptied a minute ago would be shut down on a stale total.
* A shutdown only ever follows a **live** observation of a running, empty
  server, so an outage while people are playing can never stop the server
  underneath them.
* On a VM hosting **several** Crafty servers, an expired timer stops its own
  server but leaves the VM up while any other server is still running. If that
  check cannot be made, the VM is left up: an extra hour of compute is cheaper
  than an unannounced shutdown.
* A failed shutdown is not retried on the next tick — the countdown resets, so
  it has to wait out the full idle period again. `/timeout` reports what
  happened.

Armed timeouts are written to `TIMEOUT_STATE_FILE` (`timeout_state.json` next to
the bot) so a restart does not silently leave a VM billing overnight. The
countdown deliberately restarts from zero after a restart: the bot was not
watching while it was down, so it cannot claim the server stayed empty.

Cost on a Pi Zero W: with nothing armed the watcher makes **no API calls at
all**. With a timeout armed it makes one call every `TIMEOUT_CHECK_INTERVAL`
seconds (60 s by default) while a server is up, and backs off to one every five
minutes while every armed server is stopped — no countdown can start until one
comes back, so there is nothing to watch closely.

`IDLE_SHUTDOWN_ENABLED=true` simply pre-arms this same switch for the default
server at startup, using `IDLE_SHUTDOWN_MINUTES` and `AUTO_SHUTDOWN_VM`. There
is only ever one countdown per server and one code path that stops anything. A
timeout restored from disk wins over the configured default, since it is the
more recent deliberate choice.

---

## Permissions model

| Tier | Who qualifies | Commands |
| --- | --- | --- |
| **everyone** | anyone who can use the bot | `/status`, `/health`, `/server status/players/info/resources`, `/azure status`, `/azure ip` |
| **server** | `SERVER_CONTROL_ROLE_IDS` + admins | start/stop/restart, console commands, logs, backups, scheduler |
| **azure** | `AZURE_CONTROL_ROLE_IDS` + admins | VM start/stop/deallocate |
| **admin** | `ADMIN_USER_IDS`, `ADMIN_ROLE_IDS`, Discord Administrators, guild owner | `/server kill`, `/azure restart`, `/azure stop force:true` |

If a tier has no roles configured it stays **admin-only**, so a fresh install is
locked down rather than open. Refusals are always ephemeral, and buttons the
caller may not use are not shown.

---

## Security considerations

* **Secrets stay in the environment.** Nothing is hard-coded, and `.env` should
  be `chmod 600`.
* **Tokens are never rendered.** They are sent as `Authorization` headers only —
  never in a URL, an embed, an error message or an exception. Tests assert this.
* **Logs are scrubbed.** A logging filter redacts bearer tokens and
  `client_secret`/`token`/`code` query parameters even if one slips through.
* **Error bodies are not echoed.** Azure token failures log the error *code*
  only, because the response can contain the request payload.
* **Least privilege.** A custom Azure role that can only read and power-cycle one
  VM; a Crafty API key with only the permission bits the bot uses.
* **Guild pinning.** `DISCORD_GUILD_ID` restricts both command registration and
  which guild's interactions are accepted.
* **No privileged intents.** The bot cannot read message content, and it uses no
  prefix commands.
* **Confirmation gates** on `/azure stop`, `/azure restart`, `/server kill` and
  dangerous console commands; `force` is administrator-only.
* **No local execution.** The Pi never runs Minecraft, Crafty or the Azure CLI:
  there is no subprocess and no shell in the code path, so there is nothing to
  inject into.
* **No large downloads.** Backups stay on the Crafty host; the bot only triggers
  and reports them.

---

## Resource usage and caching

Choices that matter on a 512 MB, single-core ARMv6 board:

* Slash commands only, `Intents.none()` plus guilds — no member or message cache.
* One `aiohttp` session per service with at most four connections, created lazily
  and closed on shutdown.
* No background polling by default. Crafty is queried when a command asks for it;
  the only optional background task is the idle watcher.
* Short-lived caches, sized so `/status` is never misleading:

  | Data | TTL |
  | --- | --- |
  | Server list (autocomplete) | 60 s |
  | Server configuration | 5 min |
  | Server statistics | `STATUS_CACHE_TTL` (10 s) |
  | Azure VM metadata (region, size, NIC) | 5 min |
  | Azure power state | 5 s, bypassed before every write |
  | Azure public IP | 60 s |

  Concurrent requests for the same key share one HTTP call, and any write
  invalidates the affected entries immediately. Cache locks are reference-counted
  and released with their entry, so nothing accumulates over a long uptime.
* Progress edits are throttled to one per 1.5 s.
* `/servers` costs exactly one request no matter how many servers exist.
* `/server history` charts with Unicode block characters instead of rendering an
  image — no matplotlib, no Pillow, no font stack.
* **Webhooks move work off the Pi entirely.** `/webhook create` configures Crafty
  to POST events straight to Discord, so a busy server generates announcements
  without the bot polling, holding a WebSocket, or even running.
* No database, no ORM, no web server, no Prometheus scraping, no browser.

---

## Crafty API coverage

Researched against the [official v2 API reference](https://docs.craftycontrol.com/pages/developer-guide/api-reference/v2/)
and cross-checked with the Crafty **4.10.8** source, because the published
OpenAPI document is outdated in places.

**Used**

| Endpoint | Used by |
| --- | --- |
| `GET /api/v2/crafty/check` | connectivity probe, `/health`, start workflow |
| `GET /api/v2/crafty/stats` | `/server resources`, `/status` |
| `GET /api/v2/servers` | autocomplete, server resolution |
| `GET /api/v2/servers/status` | `/servers` (unauthenticated; one call for all servers) |
| `GET /api/v2/servers/{id}` | `/server info` |
| `GET /api/v2/servers/{id}/stats` | `/status`, `/server status`, `/server players` |
| `POST /api/v2/servers/{id}/action/{action}` | start, stop, restart, kill, `backup_server/{backup_id}` |
| `POST /api/v2/servers/{id}/stdin` | `/server command` |
| `GET /api/v2/servers/{id}/logs` | `/server logs` (`?file=true` for the log file) |
| `GET /api/v2/servers/{id}/backups` | `/server backups`, default-backup lookup |
| `GET /api/v2/servers/{id}/history` | `/server history` |
| `POST /api/v2/servers/{id}/files` | `/server properties`, `/server roster` (read-only) |
| `GET /api/v2/servers/{id}/webhook` | `/webhook list` |
| `POST /api/v2/servers/{id}/webhook` | `/webhook create` |
| `PATCH/DELETE /api/v2/servers/{id}/webhook/{id}` | `/webhook toggle`, `/webhook delete` |
| `POST /api/v2/servers/{id}/webhook/{id}` | `/webhook test` |
| `GET /api/v2/servers/{id}/tasks/{taskId}` | `/schedule info` |
| `POST /api/v2/servers/{id}/tasks` | `/schedule create` |
| `PATCH/DELETE /api/v2/servers/{id}/tasks/{taskId}` | `/schedule toggle`, `/schedule delete` |
| `POST /api/v2/servers/{id}/tasks/{taskId}/run` | `/schedule run` (with `cascade` for task chains) |

**Deliberately not used**

* `GET /api/v2/servers/{id}/tasks` and `/tasks/{id}/children` — stub handlers in
  4.10.8 (`def get(...): pass`), so schedules cannot be *listed* over the API.
  Creating, inspecting, pausing, deleting and running individual tasks all work
  and are exposed; `/schedule create` reports the new task's ID, and IDs are also
  visible in the panel under *Server → Schedule*.
* **Console WebSocket** — authenticates with a browser cookie rather than an API
  key, and would require a permanently open connection. `/server logs` reads the
  same buffer over HTTP on demand, which is cheaper on a Pi Zero W.
* **File *writes*, user/role management, server creation and deletion, config
  patching** — powerful and destructive, with no natural Discord UX. They are
  intentionally out of scope; use the Crafty panel. `POST …/files` is used only
  to *read* `server.properties` and the JSON player lists.
* **`/metrics` (Prometheus)** — the bot is not a monitoring system.

Quirks handled inside `CraftyService` so the rest of the code never sees them:

* `/stats` nests everything under `data` (the spec shows it flat) — both shapes
  are accepted.
* Unknown values come back as `False` or `"False"`; they become `None` and render
  as `N/A`. A genuine `0` (zero players, 0 % CPU) is preserved.
* `players` is a text column holding a Python `repr` or JSON list — parsed
  safely, never `eval`-ed.
* `GET …/backups` returns a mapping keyed by backup ID with no `status` envelope.
* Permission failures arrive as HTTP 400 with `error: NOT_AUTHORIZED`, and some
  failures arrive as HTTP 200 with `status: error`; both map to typed exceptions.
* `/logs` HTML-escapes every line (`&quot;`, `&#x27;`, `&lt;`) because the same
  payload feeds Crafty's web terminal — the bot un-escapes them, otherwise the
  entities show up literally inside the Discord code block.
* Webhook triggers are stored as one trailing-comma string (`"a,b,c,"`) but must
  be *sent* as a JSON array, and the create schema requires at least seven of its
  eight properties while rejecting any it does not know.
* A scheduled task has both an `action` (what the panel shows) and a `command`
  (what the scheduler actually runs); Crafty's own front-end derives the second
  as `f"{action}_server"`, which `create_task` reproduces exactly.
* `POST …/files` answers 400 `DECODE_ERROR` for a file that does not exist yet,
  which becomes a `CraftyNotFound` so `/server roster` can say *"nobody is banned"*
  rather than *"the request was rejected"*.
* `GET /servers/{id}` reports `last_backup`, but the value is Crafty's
  `last_backup_failed` boolean — the bot labels it accordingly.
* `/servers/status` and `/history` do not order their rows; history samples are
  sorted before being charted.

---

## Development and tests

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

135 tests, roughly four seconds, **no network access**:

* `test_crafty_service.py` — status/start/stop/restart/players/console/logs/
  backups/scheduler, response-shape quirks, retries, and every error class
  (timeout, connection refused, 401/403/404/500, `status: error`, bad JSON),
  including an assertion that the token never appears in an error.
* `test_azure_service.py` — power state, IP lookup, start/deallocate/powerOff/
  restart, waiting, API versions, error mapping, and the credential provider
  (service-principal flow, caching, rejected credentials).
* `test_orchestrator.py` — the two headline scenarios
  (`Azure stopped → /minecraft start → …` and
  `Minecraft running → /azure stop → …`), plus forced stops, refusing to
  deallocate when Crafty is down, and degraded snapshots.
* `test_config.py`, `test_ui.py`, `test_bot.py` — configuration validation,
  permission tiers, caching, formatting, embeds, the registered command tree and
  the VM-aware Crafty gate (no request while the VM is off).

Crafty and Azure are simulated by local `aiohttp` applications built from the
real response shapes, so the tests exercise genuine HTTP behaviour while being
structurally unable to touch a real service.

---

## Migrating from the original bot

* Prefix commands (`>start`, `>stats`, `>sync`, …) are gone; everything is a
  slash command, registered automatically at startup.
* The entrypoint is `python -m bot` instead of `python core/main.py`.
* Environment variables were renamed for clarity:

  | Old | New |
  | --- | --- |
  | `SERVER_URL` | `CRAFTY_URL` |
  | `CRAFTY_TOKEN` | `CRAFTY_API_TOKEN` |
  | `GUILD_ID` | `DISCORD_GUILD_ID` |
  | `ENABLE_AUTO_STOP_SERVER` | `IDLE_SHUTDOWN_ENABLED` |
  | `AUTO_STOP_SLEEP_TIME` | `TIMEOUT_CHECK_INTERVAL` (`IDLE_CHECK_INTERVAL` is still read) |

* The standalone idle watcher became the `/timeout` switch. `IDLE_SHUTDOWN_*`
  now pre-arms that switch instead of running a second loop, so the behaviour is
  the same but it can be changed from Discord without a restart.

* `USERNAME`/`PASSWORD` login was removed. API keys are scoped, revocable and do
  not require storing an account password; MFA-protected accounts cannot log in
  through the API anyway.
* TLS certificates are now verified by default (`CRAFTY_VERIFY_SSL=true`).

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| “The Azure VM that hosts Crafty is powered off” | Expected — the bot did not even try Crafty. Run `/azure start` (VM only) or `/minecraft start` (whole stack). |
| “Crafty rejected the API token” | Key revoked, or missing the permission bit for that command. |
| Commands do not appear in Discord | `DISCORD_GUILD_ID` unset (global commands take up to an hour) or the bot was invited without `applications.commands`. |
| “Azure authentication failed” | Wrong tenant/client/secret, or the role assignment does not cover this VM. |
| “Azure VM … could not be found” | Subscription, resource group or VM name mismatch. |
| TLS errors against Crafty | Self-signed certificate: use a reverse proxy with a real certificate, or `CRAFTY_VERIFY_SSL=false` over a VPN. |
| `/server logs` returns nothing | The API key lacks `TERMINAL` (console buffer) or `LOGS` (log file). |
| `/schedule` says not authorised | The API key lacks `SCHEDULE`. |
| `/webhook` says not authorised | The API key lacks `CONFIG`. |
| `/server properties` or `/server roster` says not authorised | The API key lacks `FILES`. |
| `/server roster` says a list does not exist yet | Minecraft only writes `whitelist.json`, `ops.json` and `banned-players.json` once the list is first used. |
| `/server history` is empty | Crafty records samples only while a server runs, and keeps about an hour. |
| `/timeout` never fires | The countdown only runs while the server is **up** with zero players. `/timeout` says whether it is counting or paused, and why. |
| `/timeout` reset itself | Expected after a long gap in checks (Crafty unreachable): idle time that was not actually observed is discarded, so the count starts again from the first good reading. |
| An armed timeout disappeared after a restart | Check that `TIMEOUT_STATE_FILE` is writable by the bot's user; the state is saved next to it and a failed write is logged as a warning. |
| `/timeout` armed but the VM stayed up | Either another Crafty server on that VM is still running, or the VM check failed — both leave the VM up on purpose. The log line says which. |
| `/servers` shows fewer servers than expected | `/servers/status` only publishes servers with *Show status* enabled in Crafty. |
| Everything is slow on the Pi | Normal on first import; check `journalctl -u crafty-bot` and confirm `LOG_LEVEL=INFO`. |
| Certificate or Azure token errors right after boot | The Pi Zero W has no clock. Check `timedatectl status`; the bot needs the time to be in sync. |
| `pip` spends an hour compiling `aiohttp` | The virtualenv was created without `--system-site-packages`, so Debian's `python3-aiohttp` is invisible. Recreate it. |

`LOG_LEVEL=DEBUG` logs request paths and status codes (never tokens).

---

## Credits and licence

Originally created by [Philippe Westenfelder (Two-Play)](https://github.com/Two-Play/Crafty-Discord-bot);
this fork extends it with Azure orchestration, a service layer and a modern
Discord interface. Crafty Controller is a project of
[Arcadia Technology](https://craftycontrol.com).

Released under the [MIT licence](LICENSE.md).
