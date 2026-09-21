# envr design

Status: Vultr, UpCloud, Exoscale, and Gcore are account-backed deployment
validated. DigitalOcean, Akamai Cloud, and Scaleway Elastic Metal are implemented
against current official API contracts and locally tested. Scaleway still needs
an account-backed deployment test.

## Goal

`envr` provides one disposable cloud development VPS that can be controlled
from Android Termux, macOS, or Linux. The normal interface remains:

```bash
envr machine              # create, migrate, destroy, provider credentials
envr go [workspace]       # connect; add unknown workspaces interactively
envr go --choose          # pick, add, or remove entries
envr status
envr relay
```

There is no `init`, `connect`, `shell`, separate relay-edit/delete, or envr
authentication-maintenance command. Setup, reconnection, device enrollment,
and credential synchronization happen inside the commands above.

## Configured clouds, one VPS

An `envr` VPS is identified in each configured provider by:

```text
label: envr-codex
tag:   envr-managed
```

Legacy tags beginning with `envr-managed-` are also recognized. Finding more
than one matching normal VPS is a conflict: creation and connection refuse to
guess, `status` lists them, and machine destruction checks and offers to remove all of
them. Migration is the sole controlled exception: while it runs, one temporary
target or one retired source can coexist under distinct labels.

This provider-wide identity is deliberate. Inventory across all configured
providers must still contain at most one normal VPS. A VPS created from a Mac
must be discoverable from a phone with the same provider token; identity is
not tied to a local installation.

## Provider boundary

Vultr, UpCloud, Exoscale, Gcore, DigitalOcean, Akamai Cloud, and Scaleway adapters normalize account data,
plans, regions, Ubuntu images, instances, creation, relabel/start, and deletion.
Core SSH, cloud-init, Git, Codex, tmux, safety checks, and migration logic are
shared. Adding a provider must not fork that lifecycle.

Scaleway uses the Elastic Metal v1 API and IAM SSH keys with project-scoped
credentials. Normalized server IDs include the zone so restart, relabel, start,
and delete need no local mapping. Inventory is restricted to the configured
project; mutations verify project ownership. Three named x86 offers cover 16 GB
HDD and 32 GB SSD/NVMe. The live catalog admits only stocked hourly offers with
zero setup fee and compatible cloud-init Ubuntu 24.04. Create revalidates billing,
price, OS, and hardware, sends base64 cloud-init, and requests no paid options.
Regional fallbacks must match the displayed price and hardware. Monthly pricing
is intentionally omitted. Installation must complete before SSH setup; KVM must
pass before target promotion. Ambiguous create failures reconcile inventory and
stop rather than purchasing another server. Core migration and deletion-confirmation
rules remain shared; account-backed validation is still outstanding.

`envr machine` is the only provider configuration interface. It verifies and
stores provider API credentials, and selects the destination used by machine creation and migration.
Old state without a provider field is interpreted as Vultr. Tokens remain
separate and are never sent to a VPS.

Preferred regions are provider-specific and Frankfurt-first. Capacity fallback
is allowed only after an explicit pre-instance creation rejection. Unknown
creation outcomes never fall through to another region or provider.

## Multi-device authority

Each trusted client device has its own Ed25519 key:

```text
~/.config/envr/ssh/id_ed25519
~/.config/envr/ssh/id_ed25519.pub
```

Where the provider exposes an account SSH-key registry, the public key is
registered as `envr-device-FINGERPRINT`. New VPS instances always include the
current key and all discoverable envr device keys in the `remote` user’s
`authorized_keys`. Resize also copies that authoritative file, which keeps
devices enrolled across provider boundaries.

Provider account SSH keys do not modify an already-running instance. Therefore,
when a new device joins a running VPS:

```text
find VPS through its provider API
→ try the device key
→ fail immediately without an interactive password prompt if unauthorized
→ register the device public key where supported
→ enroll once with the VPS recovery password
→ reconnect as remote with the device key
```

If the provider supplies the instance default password through its API, envr can
instead enroll the device directly through a temporary SSH askpass file. The
password never appears in a process argument or log. If the provider omits it, envr
asks for it explicitly in the controlling terminal without an SSH probe
timeout. The provider web console can provide the password as a manual fallback.

After any key-authorized connection, envr creates or retrieves one strong
per-VPS recovery password, sets it for root through `chpasswd` standard input,
and stores it mode `600` both on the VPS and connected client. Thus envr no
longer depends on the provider continuing to expose its original provisioning
password. A new client may enter the recovery password once to install its
public key; later connections remain key-only.

SSH exit code `255` is ambiguous. envr treats only explicit authentication
messages such as `Permission denied` as authority to start password enrollment.
Timeouts, refused or filtered connections, routing failures, and changed host
keys never trigger a password prompt. TCP failure, SSH handshake closure,
host-key verification, local-key loading, and unknown pre-authentication
failure each produce a distinct error containing the final sanitized SSH
diagnostic line.

All remote SSH commands and SCP transfers within one envr invocation use
`ControlMaster=auto` with a mode-private, invocation-specific control socket
and a five-minute `ControlPersist`. This makes the first successful
authenticated connection the transport for the rest of that command without
coupling independent terminal tabs: closing one tab cannot terminate another
tab's SSH transport. Local control sockets are transient and removed with
instance state. The idempotent
initial `true` probe retries transient handshake/network failure up to three
times; explicit authentication, host-key, and local-key failures stop
immediately, and arbitrary remote operations are never automatically replayed.

Removing a device key from a provider account prevents it from being included
in future VPS instances. A key already installed on a running VPS must also be
removed there, or the disposable VPS can simply be destroyed and recreated.

## Optional SSH relay

`envr relay` configures one optional IPv4 SSH relay. With no existing
configuration it asks for:

```text
Relay IP:
SSH port [22]:
SSH user:
```

OpenSSH itself then asks for the relay password and host-key confirmation. The
interactive setup has no artificial response timeout. `envr` generates a
dedicated Ed25519 identity and installs only its public key with
`restrict,port-forwarding,command="/bin/false"`. The password is never read,
stored, placed in an argument, or logged by `envr`.

When configured, the same command displays the IP, user, port, and
`Password: not stored (key-only)`, then offers only `E`dit, `D`elete, or
`Q`uit. Editing is transactional: the old configuration remains active unless
key installation and a certificate-validated TLS connection to the selected API
through the candidate relay both succeed. Deletion is confirmed and removes
the local relay configuration, identity, host keys, and control sockets.

Relay configuration is fail-closed. Every VPS SSH/SCP operation receives one
centralized ProxyCommand; all provider API calls use SSH Unix-socket forwards
to current IPv4 API addresses. IPv4 is deliberate because an API allowlist may
permit broad IPv4 but reject the relay's otherwise preferred IPv6 address.
TLS still validates the provider API hostname and
certificate on the client, so DNS cannot expose the token or impersonate the
API. There is no direct fallback while a relay is configured. Direct and
relayed VPS transports use different control-socket names so one mode can
never accidentally reuse the other.

The relay receives no provider API token, VPS private key, recovery password,
Codex auth, Git private key, repository file, or terminal plaintext. It sees
the client address, destinations, timing, and traffic volume. Agent forwarding
is disabled. Package downloads and Git traffic initiated by the VPS do not
pass through the relay.

## Server-authoritative synchronization

While it exists, the VPS is authoritative for repositories and current Codex
authentication. It stores a portable manifest at:

```text
/home/remote/.config/envr/repos.json
```

After a successful connection, the client caches the instance ID, IP, RAM,
region, SSH host key, and its device key. Later `go` calls first try that exact
IP with strict host-key checking and key-only authentication. A successful
cached SSH connection proceeds even if the provider API is unavailable. A short,
best-effort API request may add newly registered envr device public keys to the
server, but failure never blocks the established SSH session.

Successful `status` discovery also refreshes this connection cache. When
upgrading an older installation whose state predates cached IPs, envr may
recover the address only when its private known-hosts file contains exactly one
unambiguous IPv4 host.

For every repository, the manifest contains:

```text
SSH URL
derived folder
repository identity
Git host
Git author name
Git author email
```

Repository keys remain at:

```text
/home/remote/.ssh/envr/REPOSITORY_FOLDER
```

On every connection and before destruction, `envr` atomically refreshes the
local cache from the VPS:

```text
repository manifest
repository SSH private/public keys
Codex auth.json
```

This permits the following cross-device flow without copying a local profile:

```bash
# VPS was created from macOS

# On Android
envr status
envr go envr
```

`status` is API-only and works before SSH enrollment. When configured, its API
requests also use the relay. `go` enrolls the phone, resolves `envr`, and
attaches to that repository's persistent tmux
session.

If no VPS exists, repository configuration must already be cached locally or
the user must run `envr go --choose`. Once a VPS has been destroyed, a completely new
device cannot recover credentials that existed only on that deleted VPS.

## Clean local state

The default private state directory is:

```text
~/.config/envr/
├── api                         # legacy-compatible Vultr token
├── cloud.json                  # selected provider
├── providers/
│   ├── akamai/api
│   ├── digitalocean/api
│   ├── upcloud/api
│   ├── exoscale/api            # API key + one-time secret, two lines
│   └── gcore/api
├── codex/
│   └── auth.json
├── git/
│   └── REPOSITORY_FOLDER/
│       ├── id_ed25519
│       └── id_ed25519.pub
├── ssh/
│   ├── id_ed25519
│   ├── id_ed25519.pub
│   └── known_hosts
├── relay/
│   ├── config.json
│   ├── id_ed25519
│   ├── id_ed25519.pub
│   └── known_hosts
├── repos.json
└── state.json
```

`XDG_CONFIG_HOME` is honored. `ENVR_HOME` is available for an explicit
override. On first use, known runtime files from the prototype’s `~/envr`
directory are moved into the hidden configuration directory without moving or
deleting unrelated source files.

Directories use mode `700`; tokens, state, credentials, and keys use mode
`600`. Sensitive files must not be placed in shared Android storage, Git,
messaging applications, email, or cloud notes.

## Repository configuration

`envr go --choose` takes no arguments and asks for:

```text
Repository SSH URL:
SSH private key path (leave empty to generate one):
Git author name:
Git author email:
```

Accepted URLs use the generic SCP-like SSH form:

```text
git@HOST:OWNER/REPOSITORY.git
```

No GitHub- or GitLab-specific behavior exists. The final path component
determines the folder:

```text
git@github.com:igonna/envr.git
→ envr
```

When generating a key, `envr` prints only the public key and waits for the user
to add it to the Git host. Configuration performs no connection to the Git
host from the client device.

Imported keys are copied atomically into the private configuration directory.
Repository access is tested later, only from the disposable VPS.

## `envr machine`

The exact grammar is:

```text
envr machine
```

`envr machine` is interactive: create or migrate, destroy, provider credentials,
or quit. Creation and migration ask for a live cloud configuration rather than
positional RAM arguments. It never opens an interactive shell.

If no VPS exists:

```text
query live plan, OS, and regional availability data from the selected provider
→ create in Frankfurt, then configured nearby regions only when creation is rejected
→ include all registered envr device keys
→ wait for active IPv4, SSH, and successful cloud-init
→ install and validate the standard runtime
→ restore cached Codex authentication when present
→ create the recovery password and durable initial-ready marker
→ exit with the instruction to run envr go
```

If creation was interrupted before the durable initial-ready condition:

```text
choose create or migrate in envr machine
→ locate the same VPS through trusted cache or API
→ finish bootstrap without creating a replacement
→ record initial-ready
→ exit with the instruction to run envr go
```

A healthy VPS offers migration through the same menu. Incomplete initial setup
finishes on the original VPS before migration is allowed.

Regional fallback is permitted only before the provider returns an instance ID. Once
an instance exists, `envr` never deletes it or creates a replacement because
of an SSH, cloud-init, setup, or local timeout. It reports that the VPS remains
billable, and the same `envr machine` command resumes setup. Interruption also
leaves that single instance intact.

## `envr go`

`envr go [REPOSITORY_FOLDER]` owns all interactive connection and workspace
selection. With no argument it reads the last workspace saved on the VPS. With
a repository already present, it validates only that local Git directory and
attaches immediately without a Git-host request. If the configured repository
path is absent, it synchronizes configuration, uploads that repository's
dedicated key, clones and verifies it from the VPS, records it as the last
workspace, and attaches.

An existing path is never pulled, reset, cleaned, overwritten, or switched;
its absence is the only condition that permits cloning. Repository private
keys remain repository-local and are never loaded into a global SSH agent.

`go` tries the trusted cached IP, host key, and device key before using the API.
It may start an existing stopped VPS, but it never creates, replaces, or
resizes one. With no managed VPS it fails with the single instruction to run
`envr machine`.

## `envr machine`

The exact grammar is:

```text
envr machine
```

Migration lists all supported destination clouds, configures credentials when
missing, and then queries the selected
provider's live plans, Ubuntu image, and regional availability. Its second
numbered menu contains only exact allocated RAM sizes, CPU counts, the first
available preferred region, and live price when exposed. Resize always creates
a fresh Ubuntu VPS, so the same behavior supports both larger and smaller
plans. It is not a block-device clone and does not use provider snapshots or
native plan resize. An interrupted migration is detected from its remote marker
and resumed without asking for the target size again.

The migration payload is deliberately limited to:

```text
/home/remote/envr/
/home/remote/.codex/
/home/remote/.ssh/envr/
/home/remote/.ssh/authorized_keys
/home/remote/.config/envr/repos.json
/home/remote/.config/envr/last-workspace
```

This preserves every repository and meaningful working file, complete Codex
persistent state, repository-specific keys, repository metadata, and the
directory to open after migration. For each Git repository, envr asks Git for
untracked ignored paths using `git ls-files --others --ignored
--exclude-standard`; only those paths are excluded. Tracked files and
untracked non-ignored work remain in the payload. Source-side rsync runs through
passwordless sudo so readable tracked content is not lost merely because a
container or tool created it as root; destination ownership remains `remote`.
The recovery-password record, root state, system directories, packages, VPNs,
containers, services, firewall
rules, and open ports are not copied. The destination receives the normal
fresh envr bootstrap and current Codex CLI.

Codex must not be running. migration refuses before creating a target when a
Codex process or armed supervisor record is found. Immediately before cutover,
resize takes the same lock used to create tmux sessions, checks both conditions
again, and only then stops the old tmux server. This closes the concurrent
`envr go`/`scodex resume` race. Process memory and tmux sessions cannot move
between VPSs; after `envr go` creates a fresh destination tmux, `scodex resume`
uses the copied Codex session history.

The transaction is:

```text
verify one source and requested live plan
→ measure only the migration payload
→ confirm the preservation boundary
→ mark the source as resizing
→ create a fresh temporary target with every registered device key
→ require payload size plus 1 GB to fit the target
→ generate a temporary migration key on the target
→ authorize it on the source only from the target IPv4
→ pin the source SSH host key obtained over the trusted source connection
→ rsync directly from source to target over SSH
→ checksum-dry-run every copied directory and file
→ validate repository manifests and private keys
→ refresh local repository and Codex caches from the target
→ verify destination SSH using the pinned client host key
→ relabel the source as retired
→ retire the source and promote the target
→ create the target's new recovery password and verify device access
→ delete the source and only then cache the target as authoritative
→ exit with the instruction to run envr go
```

The temporary migration identity is removed from both machines. Repository
and Codex contents travel directly between the VPSs and never through the
client or optional relay.

Before promotion, every failure leaves the source authoritative and removes
only the incomplete target when API-confirmed cleanup is possible. The source
is deleted only after transfer and destination validation. The labels
`envr-codex-resize-target` and `envr-codex-resize-source` make interruption
recovery deterministic: rerunning the same command discards an uncommitted
target, completes a validated promotion, or restores a lone retired source.
The source and target also carry role-specific resize markers; the promoted
target keeps its marker until old-source deletion is confirmed, preventing a
second device from mistaking incomplete cleanup for a finished resize.
`go` refuses when the source marker says resize is incomplete, `status` shows
the migration roles, and machine destruction refuses until resize is resolved.

## VPS bootstrap

The fixed initial server choices are:

```text
Provider:          selected with envr machine
Plan type:         provider shared/general-purpose compute
OS:                Ubuntu 24.04 LTS x64
Regions:           Frankfurt first, then provider-specific European fallbacks
Hostname:          envr
Remote user:       remote
Repository root:   /home/remote/envr
Backups:           disabled
Additional disks: none
IPv6:              disabled initially
```

Plan IDs or stable aliases are mapped only to exact allocated sizes. Vultr uses
`vhp-4c-12gb-amd` for 12 GB and `vhp-12c-24gb-amd` for 24 GB. DigitalOcean and
Akamai have no exact 12 or 24 GB shared tier, so those choices are unavailable
instead of silently allocating and charging for 16 or 32 GB. UpCloud selects its live, least-expensive
balanced Starter plans for 1–8 GB. For 16, 24, and 32 GB it uses exact balanced
Cloud Native plans with an explicit 50 GB MaxIOPS boot disk and IPv4, including
both unbundled resources in the displayed price. UpCloud has no current exact
12 GB plan, so it does not expose one. Deletion first stops the server as
required by the provider, then deletes attached storage and backups; normal
server addresses are released with the server. Exoscale's standard sizes jump
from 8 to 16 to 32 GB, so it also omits 12 and 24 GB. It discovers
the current type and Ubuntu template UUIDs live, and uses a 50 GB local root
disk. Its zones are Frankfurt, Munich, Vienna, then Zurich. Exoscale's HMAC
authentication is signed locally with the API key and secret; relay mode opens
a separate hostname-validated TLS tunnel for each zone API host. A reusable,
unbilled `envr-managed` security group permits SSH and only ports explicitly
added manually.
Gcore uses the account's single default project, discovers region IDs, flavors,
prices, and Ubuntu image IDs live, and exposes 12/24 GB only when exact flavors
exist. Its order is
Frankfurt, Amsterdam, then Warsaw. Creation uses one project SSH key, a
reusable `envr-managed` security group with unrestricted egress and explicit
ingress, and a 50 GB boot volume marked `delete_on_termination`. Its task IDs
are polled to a terminal state before lifecycle progress. Account balance is
not exposed by the Cloud API; flavor prices shown by envr are compute-only.
Plan RAM, prices when supplied, image IDs, regions, and availability are read
through the provider API before creation.

DigitalOcean uses its v2 Droplet API, Ubuntu `24.04 (LTS) x64`, the Basic shared
CPU size family, account SSH keys, and Frankfurt-first regions. A full-access
personal access token is the simplest configuration. Billing read permission
is optional for operation but required for balance data in `status`.

Provider adapters do not imply account eligibility. Akamai requires phone/SMS
verification during onboarding. UpCloud does not accept new registrations from
Russian companies and may apply low initial resource quotas. DigitalOcean
recognizes Russian billing/tax data, but none of these facts promises payment
approval, account approval, capacity, or network reachability. envr never
circumvents provider restrictions and requires truthful account data.

Cloud-init installs:

```text
git curl ca-certificates build-essential tmux jq rsync unzip
Node.js npm bubblewrap AppArmor profiles/tools Codex CLI Docker ufw fail2ban
unattended-upgrades
```

It creates the sudo-capable `remote` user, initializes the firewall once,
allows SSH, enables fail2ban, and configures Codex to keep credentials in
`~/.codex/auth.json`. On Ubuntu 24.04 it installs and loads the distribution
`bubblewrap` AppArmor profile when AppArmor is active. Existing VPSs receive
missing sandbox prerequisites and the `scodex` wrapper idempotently on
connection. Later commands never reset firewall rules.

## Remote shell and Codex

`envr go` never launches Codex. It attaches to a normal shell in a persistent,
per-workspace tmux session. Cached Codex authentication is still restored
during initial creation and refreshed locally after disconnection.

```text
no repository       → envr-workspace in /home/remote/envr
repository "project" → envr-repo-project in /home/remote/envr/project
```

envr creates the selected session only when absent, then uses
`tmux attach-session` without detaching another client. SSH loss, relay loss,
Android suspension, and network changes detach the client while the remote
session and its processes continue. The user can detach deliberately with
`Ctrl-b d`. Ordinary shells do not survive VPS reboot, process termination, or
instance destruction. A Codex session started through `scodex resume` is the
deliberate exception described below.

Before every attachment, envr enables `mouse` on the selected session and sets
the global history limit to 50,000 lines. This applies to existing sessions
without replacing `~/.tmux.conf`. In Termux, swipe/wheel events therefore enter
tmux copy mode and scroll pane history instead of becoming shell Up/Down keys.

The remote helper has exactly two operations:

```bash
scodex auth
scodex resume
```

`scodex auth` executes `codex login --device-auth`; the user approves the
URL/code in a browser. `scodex resume` selects and persistently binds the
current tmux pane to an exact session UUID, then executes:

```bash
codex resume --dangerously-bypass-approvals-and-sandbox -C DIRECTORY SESSION_UUID
```

This intentionally disables Codex approvals and sandboxing and is restricted
to the explicit wrapper command. Users can run plain `codex`, `tmux`, or any
other terminal command themselves. Plain `codex` is not supervised.

Candidate sessions are filtered by the exact repository directory using the
local Codex state database, with rollout metadata as a compatibility fallback.
Subagent and non-interactive threads are excluded. The latest eligible main
conversation binds automatically. With empty history the wrapper creates a
legacy thread on its owned backend and names it to materialize the empty rollout
without a model turn. Its temporary conversation lock is hard-linked to the
returned UUID before publication, so the lifetime guard retains the same locked
inode. The terminal resumes this exact thread and first-run supervision applies.
A stored UUID is reused thereafter;
`scodex resume --choose` offers up to ten recent eligible conversations and explicitly
rebinds the pane, and one UUID cannot be armed in two panes. Recovery therefore
never relies on recency or `--last`.

The tmux daemon and a small keeper run in `envr-tmux.service` with
`OOMPolicy=continue`. Every supervised pane has an atomic state record and an
exclusive lock. Conversation locks also prevent concurrent starts of one UUID
in different panes. If the child exits normally (including an exit through
`Ctrl-C`), or is killed
without an increased cgroup `oom_kill` counter, its record is disarmed and it
stays stopped. With positive OOM evidence, the wrapper waits for usable memory
and resumes the exact UUID in place. If the whole tmux scope is killed, the
keeper recreates the same named repository session and launches that exact
binding. A changed boot ID similarly restores sessions that were armed across a
VPS reboot. No prompt and no `/goal resume` command are injected: persisted goal
state belongs to the exact Codex thread itself. Automatic recovery is capped at
three retries in ten minutes.

Goal recovery is a separate observer inside the supervised wrapper, not a
second Codex runner. Protocol version 8 gives each wrapper one owned app-server
backend with a private socket in a mode-700 temporary directory under `/tmp`.
The TUI attaches with `--remote unix://PATH`; the observer joins that same backend
through `codex app-server proxy --sock PATH` (raw WebSocket transport).
An independent lifetime guard terminates the owned backend process group when
the wrapper exits, including SIGKILL, and retains the conversation lock until
cleanup finishes. Normal exit also removes its temporary socket directory.
No global daemon is started/restarted; temporary sockets are outside migrated
state. Killing the guard itself is outside this cleanup guarantee. OOM detection covers the
owned backend's exit as well as the TUI's exit, with the same counter guard.
It subscribes only after verifying the exact UUID is already loaded; live
`thread/resume` joins without configuration overrides. No second backend is created,
no TCP port is opened, and no user prompt is injected. Missing/incompatible
control interfaces disable this observer with a tmux notice and an atomic
private diagnostic record, without ending the user's Codex process.

Error/turn/goal events identify eligible service failures. Codex's native
`willRetry` takes precedence. Generic errors are not retried except the anchored
HTTP 502/503/504 transport diagnostic used by Codex 0.153.4. A blocked goal is
eligible only after an observed active-to-blocked transition attributed to the
same failed turn. Delays are 60/120/300/900 seconds. Usage-limit recovery checks
account limit windows and waits until exhausted windows reset, plus 30 seconds;
missing quota information waits 15 minutes. These checks do not submit prompts.

Before activation the observer checks goal identity/budget, idle/error thread
state, repository, and the newest failed turn, then rechecks the goal. User
interruption, goal replacement/pause/clear, a new turn, or completion cancels
pending recovery. Ctrl-C stops the observer for that wrapper invocation.
Only `thread/goal/set` with the exact UUID and `status: active` is sent; objective,
model, permissions, accounting, and budget are not replaced. A lost mutation
response disables the observer rather than blindly retrying an accepted command.
Codex lacks compare-and-set goal updates, so a truly simultaneous cross-client
pause remains a documented race, not a guaranteed atomic operation. The observer
reconciles the newest failed turn on startup, checking goal timestamps before
attributing a missed failure to an active/usage-limited goal. A preexisting
blocked goal requires persisted evidence of the exact goal snapshot and failed
turn; arbitrary blocked goals remain stopped and report needs-attention.
Status records include a 30-second heartbeat, wrapper PID and boot ID.
`scodex status` also checks the live pane and held wrapper lock, exposing
unsupervised or stale sessions. Runtime updates do not retrofit running plain
Codex processes; those require a normal exit and exact-UUID supervised resume. Backend integration is experimental;
tests use an isolated real backend and a loopback-only fake model provider.

Recovery checks pane ownership and never replaces a live pane using a stale
numeric pane ID. When necessary it creates a recovery window in the original
named session. Ctrl-C is handled by the foreground Codex process; supervision
waits for that process to exit before disarming. Cgroup OOM counters establish
group-level evidence, not attribution to an exact PID, so simultaneous manual
kills and OOM events in that group remain ambiguous. Real Linux OOM and reboot
validation remains an explicit production gate.

Cloud-init adds a 4 GB swap file when the root disk has at least 6 GiB free and
no swap already exists. This is emergency headroom, not a substitute for sizing
build concurrency. Resize refuses while any supervision record is armed;
destroy disarms records and intentionally stops the keeper before deletion.
`go` probes the remote supervisor protocol version and repairs an old runtime
only when needed. Runtime upgrades preserve existing Codex configuration and
existing swap files; the default configuration is created only when absent.
On an already-running legacy VPS it does not kill or replace
the existing tmux daemon; child-level supervision becomes available immediately,
and the dedicated service owns tmux after the next reboot or fresh resize.

The resulting remote
`/home/remote/.codex/auth.json` is pulled atomically after disconnection and
before destruction.

Goal recovery also classifies structured network/stream errors with absent or
transient HTTP statuses (408, 429, 500, 502, 503, 504). Authentication and other
client errors remain ineligible. The observer reconnects to its existing backend
with capped 5/15/30/60-second delays, revalidates persisted goal/turn evidence, and
preserves pending deadlines. Ambiguous goal mutation results and incompatible
protocols remain visible failures requiring attention.


## `envr status`

`status` uses configured provider APIs and can run before a device has SSH
access. One provider failure does not hide successful results from another.
When a relay is configured, every API request is tunneled through it. It shows:

```text
account balance, pending charges, available prepaid credit
estimated prepaid runway at the current plan price
instance ID, region, RAM, CPU, IP
envr recovery root password when known on this device
status, power state, hourly and monthly price
incomplete resize source/target when present
```

Vultr represents prepaid credit as a negative balance. `envr` converts the sign
and subtracts pending charges. Runway excludes taxes, overages, other services,
and the spending capacity of a linked payment card.

## `envr machine`

Before deletion:

```text
ensure device access
→ synchronize repository keys/metadata and Codex auth locally
→ inspect every repository
→ report uncommitted files, detached HEAD, missing upstreams, and commits ahead
→ warn clearly when SSH or synchronization failed
→ ask for confirmation
```

machine destruction refuses while a resize target or retired source exists. This keeps a
partially completed migration out of the older conflicting-instance deletion
path.

After confirmation, `envr` deletes every conflicting managed instance, waits
until the API confirms absence, removes obsolete SSH host entries, and clears
transient instance state. Repository credentials and Codex authentication
remain cached for the next disposable VPS.

Stopping is not exposed because providers continue reserving and billing stopped instances.

## Security boundaries

- Each provider API credential is entered independently on each trusted client,
  validated before being saved, never passed to an external process, and
  remains protected by end-to-end TLS through an optional relay.
- API access must be permitted for the client’s public IP range. Broad IPv4
  access may be necessary for mobile networks, so account 2FA and token
  rotation are important.
- Each device has a distinct SSH private key.
- A new VPS host key is accepted automatically on first contact and recorded
  in envr's private known-hosts file. A changed key is still rejected.
- Repository keys are dedicated credentials, not the user’s primary SSH key.
- Git-host access occurs only from the VPS. Its first Git host key is accepted
  into a VPS-local known-hosts file; changed keys are rejected thereafter.
- When available, the provider default password is used only before envr
  establishes its per-VPS recovery password. Recovery passwords are
  transferred only through SSH standard input, cached mode `600`, and used for
  one-time device enrollment. Temporary askpass files are mode `600` and
  deleted immediately. Normal SSH probes are key-only and noninteractive.
- Password login and direct root login remain enabled intentionally for
  cross-device recovery. UFW, fail2ban, short VPS lifetime, and a strong
  provider password reduce but do not eliminate Internet-facing risk.
- Repository code and working trees live only on the VPS. Clients retain the
  credentials needed to recreate or join it.
- Automatic backups, snapshots, and extra disks are not created. Confirmed
  instance destruction permanently removes its working filesystem according
  to the provider’s deletion model.

## Validation

Local validation includes:

```bash
python3 -m unittest discover -s tests -v
python3 -Wall -m py_compile envr tests/test_envr.py
sh -n install.sh
```

The final production gate is one funded-account sequence:

```text
configure repo on macOS
→ create VPS
→ status/connect from a fresh Termux installation
→ verify automatic device enrollment and cache synchronization
→ configure relay and verify status plus VPS SSH use it without direct fallback
→ interrupt SSH and verify both devices reattach to the persistent tmux session
→ verify scodex auth and scodex resume
→ OOM-kill a disposable supervised Codex child and verify the exact UUID returns
→ verify a normal exit and a manual kill remain stopped
→ reboot a disposable VPS and verify the armed UUID returns in the same tmux name
→ resize upward and downward, including one interrupted pre-promotion run
→ open a port
→ destroy from Android
→ recreate from a synchronized device
```

## Workspace entries

The `go --choose` menu runs locally before any server discovery. It supports
add, edit, remove, select, and quit without requiring a VPS. Local changes are
recorded in the manifest's pending map and protected from inbound sync. Folder
mappings and removals are published on connection; Git changes apply when the
corresponding repository is opened. Pending entries clear only after successful
remote application. This does not pull or push repository work.

`envr go` reconnects to the last workspace. With no last workspace, or with
`--choose`, it offers a picker, adding entries, removing entries, and the workspace root.
Removing an entry does not delete files, keys, or tmux sessions. A remote manifest
tombstone propagates removal to other devices on synchronization.
Plain folders default to `~/envr/NAME`; creation requires confirmation if absent.
Paths outside `~/envr` require an explicit warning acknowledgment because they
are not included in migration. Existing tmux sessions are attached unchanged.
The old up, resize, git, cloud, destroy, and port-open command entry points are
removed, with no aliases. Provider tokens remain local to each device.


## Private update boundary

`PrivateUpdates` owns local key handling, package authentication, source validation,
and atomic executable replacement. `Envr.update_menu` owns only menu/SSH transport
using the cached machine and strict known-host verification. Neither path starts
Codex, calls provider APIs, or synchronizes the update key to the VPS.

Envelope v1 is `ENVR-UPDATE-1\n`, followed by OpenSSL salted AES-256-CBC ciphertext,
then HMAC-SHA256 over the complete preceding envelope. Independent encryption and
MAC secrets are derived from the 256-bit shared key using distinct HMAC
labels. Generated hexadecimal keys retain their original interpretation; ordinary
UTF-8 passphrases are stretched with scrypt (N=16384, r=8, p=1, 32-byte output,
fixed domain salt `envr-update-passphrase-v1`) to agree across devices. Authentication is constant-time and precedes decryption. OpenSSL receives
its encryption secret through an inherited pipe, not argv/environment or a file;
PBKDF2 uses SHA-256, 100,000 iterations, and an explicit-compatible 8-byte salt.
OpenSSL's `enc` alone is not authenticated; the outer MAC is mandatory.
See https://docs.openssl.org/3.0/man1/openssl-enc/.

The authenticated Python source supplies the version without being executed.
Only a higher semantic version replaces differing contents. No release registry,
key recovery mechanism, or server-side decryption is involved. Losing the key
means replacing it and publishing anew from a trusted local installation.


Service maintenance recovery records a generation in ExecStopPost, after the
service process group has stopped. Armed orphaned sessions with an older generation
are eligible for the existing bounded exact-session restart path, even on the same
boot without OOM evidence. Normal exits remain disarmed. needrestart excludes only
envr-tmux.service from automatic restarts; security updates remain enabled.


Nested-KVM choices extend plan identity without changing RAM-size aliases.
KVM_PLAN_IDS is a small provider-specific allowlist of local-disk VX1 plans;
menu availability is still checked against live plan and regional APIs. Explicit
plan_id flows through creation and both migration paths. Atomic migration markers
optionally append the selected plan ID, retaining compatibility with legacy
role:size markers. Resume retains that plan even for same-size family changes.
KVM admission is a separate post-provision/pre-promotion check using the remote
user's device access and KVM_GET_API_VERSION / KVM_CREATE_VM, with persistent module
and group setup. Provider claims are distinguished from experimental features;
no existing host is modified during menu discovery.
