# envr

`envr` is a small command-line tool for using one disposable cloud development
VPS from Android Termux. It supports Vultr, DigitalOcean, Akamai Cloud,
UpCloud, Exoscale, and Gcore and
prepares Git repositories, remote shells, Codex
authentication, development tools, and safe instance cleanup.

Status: Vultr is account-backed deployment validated. Exoscale and Gcore have
funded accounts; their adapters, along with DigitalOcean, Akamai Cloud, and
UpCloud, are contract-tested against current official APIs and await their
first account-backed envr deployment.

## Interface

```bash
envr git
envr relay
envr cloud
envr up 4
envr go
envr go repository-folder
envr resize 4
envr status
envr port-open 443
envr destroy
```

All configured cloud accounts together manage at most one `envr` VPS. Multiple repositories and
trusted client devices share it. Each client has its own SSH key; the running
VPS is authoritative for repository configuration and Codex authentication.

## Intended workflow

Configure a repository once, on any device:

```bash
envr git
```

This stores the repository configuration and dedicated key locally but does
not contact the Git host. Clone and access verification happen only from the
disposable VPS during the first `envr go repository-folder`.

Create the VPS once:

```bash
envr up 4
```

`up` performs initial bootstrap and exits. If setup was interrupted, repeating
the same command resumes that VPS. Once it is healthy, `up` refuses and points
to `go` or `resize`; it never becomes a second connection or resizing path.

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
VPS exists, it tells you to run `envr up RAM`.

`envr` never starts Codex automatically. Inside the remote shell:

```bash
scodex auth
scodex resume
```

`scodex auth` starts device authentication. `scodex resume` resumes Codex with
approvals and sandboxing explicitly bypassed; use it only because this VPS is
disposable and isolated for that purpose.

To join from another Mac, Linux machine, or Android phone, install `envr`, enter
the API token for the cloud containing the VPS, and run the same command. `envr` discovers the
running VPS and registers that device's public key. If the provider does not expose
the running VPS password, `envr` asks once for its root SSH password. It
installs the new device's public key and immediately reconnects key-only.
On disconnection, the new device receives the current repository configuration,
dedicated Git keys, and Codex auth cache from the VPS.

Initial `up` establishes one strong recovery password for that
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
envr cloud
```

The selected provider is where `envr up RAM` creates a new VPS and where
`envr resize RAM` places its destination. Selecting an unconfigured provider
asks once for its API credentials, verifies them, and stores them privately. Existing
installations remain selected as Vultr without a migration step. The same menu
can rotate credentials or forget an unused provider locally; it refuses to forget
the provider recorded as owning the running VPS.

To move between providers without another migration command:

```bash
envr cloud       # select any configured destination cloud
envr resize 32   # encrypted VPS-to-VPS migration
```

Frankfurt is preferred, with nearby European regions as fallbacks. Requested
sizes are 1, 2, 4, 8, 12, 16, 24, and 32 GB. UpCloud supplies exact custom
12 and 24 GB configurations. Gcore selects an exact live flavor where present,
or the smallest safe larger flavor. Providers without 12 or 24 GB plans round
up to 16 or 32 GB and show the actual size before creation or migration. The
old VPS remains authoritative until the destination validates and source
deletion is confirmed.

For the simplest DigitalOcean setup, create a personal access token with full
access; envr stores it mode `600`. A restricted token needs account, Droplet,
image, size, region, SSH-key, tag, and billing read/write scopes needed by the
lifecycle. If billing read is omitted, envr still works but `status` reports
that billing is unavailable.

Provider signup constraints are separate from API support. Akamai currently
requires SMS phone verification during onboarding. UpCloud's published account
requirements exclude customers located in Russia, so it is not a suitable
choice when that restriction applies. DigitalOcean is the more practical next
provider to test, but payment acceptance, account review, capacity, and access
from a particular Russian network still cannot be guaranteed. Always use
truthful identity and billing information.

For Exoscale, open the portal's IAM section, create an API key (an unrestricted
key is simplest for this disposable environment), and copy both values shown:
the API key and the API secret. The secret is shown only once. Select Exoscale
in `envr cloud`; it asks for both values without echoing them, validates them,
and saves them together in `~/.config/envr/providers/exoscale/api` with mode
`600`. Never paste either credential into chat. Exoscale uses Frankfurt first,
then Munich, Vienna, and Zurich. Its reusable `envr-managed` security group is
free and admits SSH plus ports explicitly opened with `envr port-open`.
Exoscale's compute catalog does not expose prices through this API, so `status`
shows its balance but marks per-instance price and runway unavailable.

For Gcore, create one permanent API key in the portal and select Gcore in
`envr cloud`; envr asks for the key without echoing it, validates it, and saves
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

Move the complete working environment to a fresh supported VPS size:

```bash
envr resize 24
```

Supported requested sizes are 1, 2, 4, 8, 12, 16, 24, and 32 GB. Vultr's
24 GB option uses its 12-vCPU High Performance AMD shared-CPU plan. UpCloud
uses exact custom 12 and 24 GB configurations, while Gcore chooses from its
live regional flavor catalog. A provider lacking an exact 12 or 24 GB option
uses the smallest configured safe larger size; the actual size is shown before
creation or migration.

Resize preserves `/home/remote/envr`, the complete `~/.codex` directory,
repository keys, enrolled device SSH keys, repository metadata, and the last selected workspace. Inside
Git repositories, untracked paths ignored by Git are deliberately omitted;
tracked files and untracked non-ignored work are preserved. The
operating system and standard envr tools are rebuilt fresh. Custom packages,
VPNs, containers, services, firewall rules, and open ports are not migrated.
Codex must be exited first; process memory and the old tmux process cannot be
moved, but `scodex resume` sees the copied Codex sessions afterward.

The old VPS remains authoritative until a direct encrypted VPS-to-VPS copy
passes checksum, repository-key, metadata, and destination-SSH validation.
Only then is the new VPS promoted and the old one deleted. If the command is
interrupted, rerunning the same `envr resize RAM` safely restarts an
uncommitted copy or finishes an already validated promotion. `status` exposes
an incomplete resize and `destroy` refuses to run until it is resolved. Resize
then exits; `envr go` reconnects to the preserved last workspace.

Open a service port when needed:

```bash
envr port-open 443
```

After committing and pushing work:

```bash
envr destroy
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
