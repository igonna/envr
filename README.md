# envr

`envr` is a small command-line tool for using one disposable cloud development
VPS from Android Termux. It supports Vultr, UpCloud, Exoscale, Gcore,
DigitalOcean, and Akamai Cloud and
prepares Git repositories, remote shells, Codex
authentication, development tools, and safe instance cleanup.

Status: Vultr, UpCloud, Exoscale, and Gcore have passed account-backed create,
SSH, cloud-init, tmux reconnect, destroy, and resource-cleanup tests.
DigitalOcean and Akamai Cloud are contract-tested against current official APIs.

## Interface

```bash
envr machine              # create, migrate, destroy, provider credentials
envr go [workspace]       # connect; add unknown workspaces interactively
envr go --choose          # pick, add, or remove entries
envr status
envr relay
```

All configured cloud accounts together manage at most one `envr` VPS. Multiple repositories and
trusted client devices share it. Each client has its own SSH key; the running
VPS is authoritative for repository configuration and Codex authentication.

## Intended workflow

Create a machine first, then run `envr go project`. For an unknown name,
choose Git repository or remote folder. Git configuration asks for its SSH URL,
key, and author identity; the saved name is deduced from the URL. Access is
verified only from the VPS when cloning, never from your client device.

Create the VPS once:

```bash
envr machine
```

`envr machine` offers creation or migration, destruction, and provider credentials.
Creation asks for a provider and a live configuration, then bootstraps and exits.
An interrupted setup is resumed on the same VPS. Migration retains the existing
transactional transfer and validation checks; destruction still asks for confirmation.

Connect afterward:

```bash
envr go                    # last selected workspace
envr go repository-folder  # clone if absent, otherwise attach immediately
```

`go` tries the cached IP and trusted SSH host key before contacting a cloud API. If
the VPS is reachable, an API outage does not prevent reconnection. Stale or
untrusted cached connections fail closed and fall back to live discovery.

The normal prompt is short:

```text
remote@envr:~/envr$
```

The shell runs inside `tmux`. A lost SSH connection, Android suspension, relay
failure, or network change detaches the client without stopping remote work.
Detach deliberately with `Ctrl-b d`; the next identical `go` attaches again.
Mouse mode is enabled for every session so Termux swipe scrolling enters tmux
history instead of sending shell Up/Down keys.
The general workspace and every repository use separate sessions:

```text
envr-workspace
envr-repo-REPOSITORY_FOLDER
```

`go` never creates or resizes a VPS. It tries the trusted local SSH cache first,
does not repeat bootstrap or Git-host verification for an existing repository,
and uses cloud APIs only as a fallback. If the selected repository is absent,
that one call uploads its configured key, clones, verifies, and attaches. If no
VPS exists, it tells you to run `envr machine`.

`envr` never starts Codex automatically. Inside the remote shell:

```bash
scodex auth
scodex resume
```

`scodex auth` starts device authentication. `scodex resume` resumes Codex with
approvals and sandboxing explicitly bypassed; use it only because this VPS is
disposable and isolated for that purpose. With no saved conversation in the
repository, it starts a new conversation using the same bypass flag. That first
run is not automatically recovered: exact-thread supervision begins on a later
resume, once the conversation has a saved UUID. Explicit missing UUIDs still fail
rather than silently starting a different conversation. On first resume it binds
the tmux pane to the latest main interactive conversation in that repository,
excluding subagents, and remembers its exact UUID. `scodex resume --choose`
offers the ten most recent eligible conversations to change the binding later.

That binding is supervised. A normal Codex exit, including exiting with
`Ctrl-C`, leaves it stopped. Ctrl-C handled internally by Codex does not abandon
the running process. If Linux records an out-of-memory kill and Codex is killed, envr waits
for memory to recover and resumes only that UUID in the same named tmux session.
It also restores an armed session after a VPS reboot. Recovery never uses
`--last`, never injects a prompt, and never runs `/goal resume`; an active goal
remains part of the resumed Codex thread. Recovery stops after three repeated
OOM restarts in ten minutes. Plain `codex` remains completely unsupervised.

On Codex 0.153.4, supervised resumes also watch the same live backend for
temporary service failures and usage limits. Recognized capacity/server failures
retry after 1, 2, 5, then 15 minutes; usage exhaustion waits for the reported
reset plus a margin and rechecks availability. No model prompts are used for
polling. A backend-generated `blocked` goal is recoverable only when correlated
with that exact turn's recognized service error. User pauses, genuine blockers,
completed goals, exhausted goal budgets, and unknown errors remain stopped.
Ctrl-C disables this monitor for the current `scodex` invocation; a later
`scodex resume` starts monitoring again. This does not change the OOM keeper.

Each supervised resume owns one backend through a private socket in `/tmp`,
outside the directories copied during migration. Its terminal and monitor join
that same backend; no global daemon is started or restarted and no TCP port is
exposed. A small lifetime guard cleans up the owned backend even if the wrapper
is killed, retaining the conversation lock until cleanup finishes. Normal exit
also removes its temporary socket directory.
It uses the experimental app-server protocol,
so an incompatible CLI/control connection disables goal recovery without closing
Codex. A tmux notice and private status record under
`~/.config/envr/supervision/goal-recovery/` make that failure visible.
It watches new failures after subscribing, not arbitrary old stalled sessions.
It does not recover every possible hang. Pause events and state are checked
before recovery, but Codex currently has no atomic conditional goal-update API:
a simultaneous pause from another client remains a protocol-level race.

Local tests include an actual Codex backend, a loopback fake capacity response,
and two clients verifying continuation on the same thread. Run the optional
installed-binary tests with `ENVR_TEST_CODEX_PROTOCOL=1 python3 -m unittest discover -s tests -p test_goal_recovery.py`.
No real account credentials or model requests are used by those tests.

Codex is installed as `remote` under `~/.local`, with npm's user prefix set
there, so its built-in update action works without sudo. `scodex` uses that
same installation even inside an older tmux shell.

Recovery has automated process simulations, but a real Linux OOM/reboot test
remains required before treating it as production-verified. OOM counters cover
the service group: a simultaneous manual kill and unrelated OOM in that same
group cannot be distinguished reliably by this mechanism.

To join from another Mac, Linux machine, or Android phone, install `envr`, enter
the API token for the cloud containing the VPS, and run the same command. `envr` discovers the
running VPS and registers that device's public key. If the provider does not expose
the running VPS password, `envr` asks once for its root SSH password. It
installs the new device's public key and immediately reconnects key-only.
On disconnection, the new device receives the current repository configuration,
dedicated Git keys, and Codex auth cache from the VPS.

Initial machine creation establishes one strong recovery password for that
VPS, synchronizes it to the connected device with mode `600`, and durably
records the IP, device key, and trusted SSH host key. An authorized device can
show the recovery password with `envr status`; a new device enters it once,
then all later connections are key-only and can proceed without a cloud API.
Only an explicit SSH authentication rejection can request that password.
Timeouts, filtered routes, and other network failures instead report that the
VPS is unreachable; changing VPN or mobile networks never removes a device
key. TCP reachability, SSH handshake closure/reset, host-key verification, and
local private-key failures are reported separately with the final sanitized
SSH diagnostic.

Within one `go`, SSH and SCP share a private OpenSSH control connection. Once
one key-authenticated handshake succeeds, workspace selection, the interactive
shell, and the cache refresh after disconnect reuse that connection instead of
repeatedly crossing an unreliable or filtered network path.
Each envr invocation has its own control connection, so independent terminal
tabs do not depend on one another and closing one does not disconnect another.
Transient failures of the initial safe key probe are retried up to three times;
authentication, host-key, and local-key failures are never blindly retried.

Keys under `~/.ssh/envr` are never loaded globally or placed in an agent; each
repository remains bound only to its own dedicated key. Initial setup and
resize validation repair these repository-local bindings when necessary.

## Cloud selection

Configure or select a provider with one interactive command:

```bash
envr machine
```

The selected provider is where `envr machine` creates a new VPS. Selecting an unconfigured provider
asks once for its API credentials, verifies them, and stores them privately. Existing
installations remain selected as Vultr without a migration step. The same menu
can rotate credentials or forget an unused provider locally; it refuses to forget
the provider recorded as owning the running VPS. Provider menus use the deliberate
best-fit order Vultr, UpCloud, Exoscale, Gcore, DigitalOcean, then Akamai Cloud;
this reflects tested value and operational friction for this workflow, not a
claim that one cloud is universally superior.

Resize chooses its destination directly, without changing the default first:

```bash
envr machine
```

The command lists configured clouds, queries the chosen provider's live plan,
Ubuntu-image, and regional availability, then lists only actual deployable RAM,
CPU, preferred region, and price. Rounded aliases are not shown as separate
sizes. Frankfurt is preferred, with nearby European regions as fallbacks. The
old VPS remains authoritative until the destination validates and source
deletion is confirmed.

For the simplest DigitalOcean setup, create a personal access token with full
access; envr stores it mode `600`. A restricted token needs account, Droplet,
image, size, region, SSH-key, tag, and billing read/write scopes needed by the
lifecycle. If billing read is omitted, envr still works but `status` reports
that billing is unavailable.

Provider signup constraints are separate from API support. Akamai currently
requires SMS phone verification during onboarding and did not accept the test
registration. Payment acceptance, account review, capacity, quotas, and access
from a particular Russian network still cannot be guaranteed. Always use
truthful identity and billing information.

For UpCloud, create a Bearer API token under **Account → API Tokens** and select
UpCloud in `envr machine`; token values begin with `ucat_`. UpCloud uses Frankfurt,
Amsterdam, Warsaw, then Helsinki, reads prepaid balance and USD plan prices live,
and deletes the boot storage and backups with the server. Current Starter plans
are selected for 1–8 GB because they are the least expensive balanced bundled
options. Exact 16, 24, and 32 GB Cloud Native plans receive a 50 GB MaxIOPS boot
disk and public IPv4; their displayed price includes those unbundled resources.
UpCloud currently exposes no exact 12 GB plan, so that size is not shown. New
accounts can have low core/RAM quotas even after funding; larger deployments may
require a quota increase.

For Exoscale, open the portal's IAM section, create an API key (an unrestricted
key is simplest for this disposable environment), and copy both values shown:
the API key and the API secret. The secret is shown only once. Select Exoscale
in `envr machine`; it asks for both values without echoing them, validates them,
and saves them together in `~/.config/envr/providers/exoscale/api` with mode
`600`. Never paste either credential into chat. Exoscale uses Frankfurt first,
then Munich, Vienna, and Zurich. Its reusable `envr-managed` security group is
free and admits SSH. Additional service ports are managed manually.
Exoscale's compute catalog does not expose prices through this API, so `status`
shows its balance but marks per-instance price and runway unavailable.

For Gcore, create one permanent API key in the portal and select Gcore in
`envr machine`; envr asks for the key without echoing it, validates it, and saves
it in `~/.config/envr/providers/gcore/api` with mode `600`. The account's one
default project is selected automatically. Gcore uses Frankfurt, Amsterdam,
then Warsaw, live flavor prices, a managed security group, and a 50 GB boot
volume deleted with the instance. Gcore's Cloud API does not expose account
balance through this adapter, so `status` marks balance/runway unavailable;
the displayed instance price is compute-only and storage or public-IP charges
may be additional.

## Optional SSH relay

When the direct path to a provider is filtered or unreliable, configure one stable
SSH server:

```bash
envr relay
```

The first setup asks for its IPv4 address, SSH port, and user. OpenSSH asks for
the relay password once while installing a dedicated forwarding-only public
key; the password is never received or stored by `envr`. The relay host key is
confirmed once and then pinned.

If a relay already exists, the same command shows:

```text
Relay
  IP:       192.0.2.40
  User:     relay
  Port:     22
  Password: not stored (key-only)

[E] Edit  [D] Delete  [Q] Quit:
```

There are no separate relay-edit or relay-delete commands. With a relay
configured, all `envr` traffic uses it: each cloud HTTPS API travels
through an IPv4 SSH forward with end-to-end hostname-validated TLS, and VPS
SSH/SCP uses an end-to-end SSH connection through the relay. There is no
silent direct fallback. Git clones and package downloads initiated by the VPS
remain direct from the VPS.

Inspect configured accounts, instances, live price when the provider exposes
it through its API, IP, and envr-managed recovery
root password:

```bash
envr status
```

Choose a cloud and move the complete working environment to one of its live
configurations:

```bash
envr machine
```

The numbered menu shows only actual sizes available from the selected provider
in the preferred regions. It never presents a 12 GB request that secretly
allocates 16 GB, or a 24 GB request that allocates 32 GB. Missing exact sizes
are named below the menu. Vultr currently provides exact 12 and 24 GB plans;
other providers expose those sizes only when their live catalogs genuinely do.

Resize preserves `/home/remote/envr`, the complete `~/.codex` directory,
repository keys, enrolled device SSH keys, repository metadata, and the last selected workspace. Inside
Git repositories, untracked paths ignored by Git are deliberately omitted;
tracked files and untracked non-ignored work are preserved. The
operating system and standard envr tools are rebuilt fresh. Custom packages,
VPNs, containers, services, firewall rules, and open ports are not migrated.
Codex must be exited first; an armed supervised session also makes resize
refuse. Process memory and the old tmux process cannot be moved, but `scodex
resume` sees the copied Codex sessions afterward. Resize locks tmux creation,
rechecks that Codex is stopped, and only then pauses tmux for the final copy.

The old VPS remains authoritative until a direct encrypted VPS-to-VPS copy
passes checksum, repository-key, metadata, and destination-SSH validation.
Only then is the new VPS promoted and the old one deleted. If the command is
interrupted, rerunning `envr machine` detects the saved target size and safely
restarts an uncommitted copy or finishes an already validated promotion. `status` exposes
an incomplete resize and machine destruction refuses to run until it is resolved. Resize
then exits; `envr go` reconnects to the preserved last workspace.

After committing and pushing work:

```bash
envr machine
```

Before deletion, `envr` preserves refreshed Codex credentials and reports
uncommitted changes, unpushed commits, missing upstreams, and detached branches
for every repository on the VPS.

## Install

From this checkout:

```bash
sh install.sh
```

On Termux, the installer supplies Python and OpenSSH when needed and
places `envr` in `$PREFIX/bin`. Linux and macOS use `~/.local/bin`.

## Implementation

The client uses Python 3.10 or newer with the standard library only. A
small POSIX installer handles local Python and OpenSSH prerequisites.
Termux is the primary client; Linux and macOS are secondary supported Unix
clients.

The complete behavior, security model, failure handling, and implementation
contract are specified in [DESIGN.md](DESIGN.md).

## Security

Runtime credentials and state live under `~/.config/envr` with restrictive
filesystem permissions. They must never be committed or placed in Android
shared storage. Existing state from the prototype’s `~/envr` layout is
automatically migrated on first use.

Each device stores only its provider API credentials, device SSH key, and a synchronized cache
of repository credentials and Codex authentication. Repository working trees,
shell tools, builds, and containers remain on the VPS.

An optional relay receives no cloud API token, VPS private key, recovery password,
Codex authentication, Git key, or repository data. It can observe connection
destinations, timing, and traffic volume, but cloud APIs remain protected by
TLS and VPS sessions remain protected by their inner SSH connection. SSH agent
forwarding is disabled.

The VPS is hardened during its initial cloud-init run. Later `envr` commands do
not reset its firewall. Automatic backups, snapshots, and additional storage
are not used, and confirmed instance destruction removes transient local
credentials.

## Local verification

```bash
python3 -m unittest discover -s tests -v
python3 -Wall -m py_compile envr tests/test_envr.py
sh -n install.sh
```

## Slow-network behavior

Initial package installation waits for APT locks and retries up to three times.
Bootstrap stops on failure instead of continuing with missing tools. A known
initial APT-lock failure can be recovered on the same VPS; initialized machines
and running Codex sessions are protected from setup replay. Recovery does not
rerun provider vendor scripts or erase cloud-init's historical failure logs.

For an existing, ready workspace, `go` checks readiness, migration state,
runtime, and the workspace in one read-only SSH request. Saving the selected
workspace happens in the tmux attachment request. Missing setup or workspaces
use the normal setup flow; authentication and host-key checks are not skipped.
SSH connections remain independent between invocations/tabs. Machine availability
checks fetch independent regions concurrently and retain the configured priority.
First-time Git setup also batches key permissions and known-host-file preparation.

## Workspace entries

`envr go --choose` opens local workspace configuration, even without a VPS:
select a workspace number, then Connect, Edit, Remove configuration, or Back.
Add and Quit are on the workspace list. Configuration does not access
Git hosts or a cloud API. Pending local edits survive synchronization; Git edits
are applied when that repository is opened, without pulling or pushing project code.
Removals and folder mappings are sent on the next successful connection.
`envr go` reconnects to the last workspace, or offers a picker if none is selected.
Removing an entry does not delete files, keys, or tmux sessions. A remote manifest
tombstone propagates removal to other devices on synchronization.
Plain folders default to `~/envr/NAME`; creation requires confirmation if absent.
Paths outside `~/envr` require an explicit warning acknowledgment because they
are not included in migration. Existing tmux sessions are attached unchanged.
The old up, resize, git, cloud, destroy, and port-open command entry points are
removed, with no aliases. Provider tokens remain local to each device.
