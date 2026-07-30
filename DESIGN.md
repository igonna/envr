# envr design

Status: implemented and locally tested; funded-account live validation pending.

## Goal

`envr` provides one disposable Vultr development VPS that can be controlled
from Android Termux, macOS, or Linux. The normal interface remains:

```bash
envr git
envr relay
envr up 4
envr up 4 repository-folder
envr status
envr port-open 443
envr destroy
```

There is no `init`, `connect`, `shell`, separate relay-edit/delete, or envr
authentication-maintenance command. Setup, reconnection, device enrollment,
and credential synchronization happen inside the commands above.

## One account, one VPS

An `envr` VPS is identified account-wide by:

```text
label: envr-codex
tag:   envr-managed
```

Legacy tags beginning with `envr-managed-` are also recognized. Finding more
than one matching VPS is a conflict: `up` and `port-open` refuse to guess,
`status` lists them, and `destroy` checks and offers to remove all of them.

This account-wide identity is deliberate. A VPS created from a Mac must be
discoverable from a phone using the same Vultr account; identity is not tied to
a local installation.

## Multi-device authority

Each trusted client device has its own Ed25519 key:

```text
~/.config/envr/ssh/id_ed25519
~/.config/envr/ssh/id_ed25519.pub
```

The public key is registered in the Vultr account as
`envr-device-FINGERPRINT`. New VPS instances include every registered envr
device key in the `remote` user’s `authorized_keys`.

Vultr account SSH keys do not modify an already-running instance. Therefore,
when a new device joins a running VPS:

```text
find VPS through Vultr API
→ try the device key
→ fail immediately without an interactive password prompt if unauthorized
→ register the device public key in the Vultr account
→ run envr up once from an already-authorized device
→ synchronize every registered envr public key to remote authorized_keys
→ reconnect as remote with the device key
```

If Vultr supplies the instance default password through its API, envr can
instead enroll the device directly through a temporary SSH askpass file. The
password never appears in a process argument or log. If Vultr omits it, envr
asks for it explicitly in the controlling terminal without an SSH probe
timeout. Trusted-device synchronization remains a passwordless alternative;
the Vultr web console can provide the password as a manual fallback.

After any key-authorized connection, envr creates or retrieves one strong
per-VPS recovery password, sets it for root through `chpasswd` standard input,
and stores it mode `600` both on the VPS and connected client. Thus envr no
longer depends on Vultr continuing to expose its original provisioning
password. A new client may enter the recovery password once to install its
public key; later connections remain key-only.

SSH exit code `255` is ambiguous. envr treats only explicit authentication
messages such as `Permission denied` as authority to start password enrollment.
Timeouts, refused or filtered connections, routing failures, and changed host
keys never trigger a password prompt. TCP failure, SSH handshake closure,
host-key verification, local-key loading, and unknown pre-authentication
failure each produce a distinct error containing the final sanitized SSH
diagnostic line.

All remote SSH commands and SCP transfers for the same VPS use
`ControlMaster=auto` with a mode-private control socket and a five-minute
`ControlPersist`. This makes the first successful authenticated connection the
transport for the rest of setup, synchronization, and shell attachment. Local
control sockets are transient and removed with instance state. The idempotent
initial `true` probe retries transient handshake/network failure up to three
times; explicit authentication, host-key, and local-key failures stop
immediately, and arbitrary remote operations are never automatically replayed.

Removing a device key from the Vultr account prevents it from being included
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
key installation and a certificate-validated TLS connection to the Vultr API
through the candidate relay both succeed. Deletion is confirmed and removes
the local relay configuration, identity, host keys, and control sockets.

Relay configuration is fail-closed. Every VPS SSH/SCP operation receives one
centralized ProxyCommand; all Vultr API calls use an SSH Unix-socket forward
to a current IPv4 address for `api.vultr.com:443`. IPv4 is deliberate because
the Vultr API allowlist may permit broad IPv4 but reject the relay's otherwise
preferred IPv6 address. TLS still validates the `api.vultr.com` hostname and
certificate on the client, so DNS cannot expose the token or impersonate the
API. There is no direct fallback while a relay is configured. Direct and
relayed VPS transports use different control-socket names so one mode can
never accidentally reuse the other.

The relay receives no Vultr API token, VPS private key, recovery password,
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
region, SSH host key, and its device key. Later `up` calls first try that exact
IP with strict host-key checking and key-only authentication. A successful
cached SSH connection proceeds even if the Vultr API is unavailable. A short,
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
envr up 4 envr
```

`status` is API-only and works before SSH enrollment. When configured, its API
requests also use the relay. `up` enrolls the phone, downloads the server
manifest, resolves `envr`, and attaches to that repository's persistent tmux
session.

If no VPS exists, repository configuration must already be cached locally or
the user must run `envr git`. Once a VPS has been destroyed, a completely new
device cannot recover credentials that existed only on that deleted VPS.

## Clean local state

The default private state directory is:

```text
~/.config/envr/
├── api
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

`envr git` takes no arguments and asks for:

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

## `envr up`

The exact grammar is:

```text
envr up RAM [REPOSITORY_FOLDER]
```

`RAM` is required and must be `1`, `2`, `4`, or `8`. The repository is
optional. Without it, the shell starts in `/home/remote/envr`.

If a VPS exists:

```text
verify requested RAM
→ start it if stopped (billing already continues while stopped)
→ ensure this device has SSH access
→ wait for successful cloud-init
→ repair missing runtime helpers/packages if needed
→ synchronize server metadata and credentials locally
→ when supplied, resolve and verify the repository from the VPS
→ attach to the selected persistent tmux session
```

If no VPS exists:

```text
when supplied, require its saved configuration locally
→ otherwise proceed without Git configuration
→ query live plan, OS, and regional availability data
→ ask Vultr to create in fra, then ams, then waw only when creation is rejected
→ include all registered envr device keys
→ wait for active IPv4, SSH, and successful cloud-init
→ restore Codex authentication
→ when supplied, upload its dedicated key and clone/verify it from the VPS
→ print one concise repository-ready message
→ attach to the selected persistent tmux session
```

An existing repository is never pulled, reset, cleaned, overwritten, or
switched automatically. Its `origin` must match the configured URL.

Regional fallback is permitted only before Vultr returns an instance ID. Once
an instance exists, `envr` never deletes it or creates a replacement because
of an SSH, cloud-init, setup, or local timeout. It reports that the VPS remains
billable, and the same `envr up RAM` command resumes setup. Interruption also
leaves that single instance intact.

## VPS bootstrap

The fixed initial server choices are:

```text
Provider:          Vultr
Plan type:         regular shared CPU
OS:                Ubuntu 24.04 LTS x64
Regions:           fra, ams, waw
Hostname:          envr
Remote user:       remote
Repository root:   /home/remote/envr
Backups:           disabled
Additional disks: none
IPv6:              disabled initially
```

Plan IDs are mapped for the four supported sizes, but their existence, RAM,
prices, OS ID, locations, and current availability are verified through the
live API before creation.

Cloud-init installs:

```text
git curl ca-certificates build-essential tmux jq rsync unzip
Node.js npm bubblewrap Codex CLI Docker ufw fail2ban unattended-upgrades
```

It creates the sudo-capable `remote` user, initializes the firewall once,
allows SSH, enables fail2ban, and configures Codex to keep credentials in
`~/.codex/auth.json`. Existing VPSs receive missing `bubblewrap` and the
`scodex` wrapper idempotently on connection. Later commands never reset
firewall rules.

## Remote shell and Codex

`envr up` never launches Codex. It attaches to a normal shell in a persistent,
per-workspace tmux session. Cached Codex authentication is still restored
before connection.

```text
no repository       → envr-workspace in /home/remote/envr
repository "project" → envr-repo-project in /home/remote/envr/project
```

`tmux new-session -A` permits multiple devices to attach without terminating
another client. SSH loss, relay loss, Android suspension, and network changes
detach the client while the remote session and its processes continue. The
user can detach deliberately with `Ctrl-b d`. Sessions do not survive VPS
reboot, process termination, out-of-memory failure, or instance destruction.

The remote helper has exactly two operations:

```bash
scodex auth
scodex resume
```

`scodex auth` executes `codex login --device-auth`; the user approves the
URL/code in a browser. `scodex resume` executes:

```bash
codex resume --dangerously-bypass-approvals-and-sandbox
```

This intentionally disables Codex approvals and sandboxing and is restricted
to the explicit wrapper command. Users can run plain `codex`, `tmux`, or any
other terminal command themselves.

The resulting remote
`/home/remote/.codex/auth.json` is pulled atomically after disconnection and
before destruction.

## `envr status`

`status` uses only the Vultr API and can run before a device has SSH access.
When a relay is configured, the API request is tunneled through it. It shows:

```text
account balance, pending charges, available prepaid credit
estimated prepaid runway at the current plan price
instance ID, region, RAM, CPU, IP
Vultr default root password
status, power state, hourly and monthly price
```

Vultr represents prepaid credit as a negative balance. `envr` converts the sign
and subtracts pending charges. Runway excludes taxes, overages, other services,
and the spending capacity of a linked payment card.

## `envr port-open`

```text
envr port-open PORT
```

The port must be `1..65535`. The command enrolls the current device if needed,
then idempotently opens both TCP and UDP in UFW and verifies the rules. It does
not alter a Vultr Firewall group or reset any existing VPS firewall rules.

## `envr destroy`

Before deletion:

```text
ensure device access
→ synchronize repository keys/metadata and Codex auth locally
→ inspect every repository
→ report uncommitted files, detached HEAD, missing upstreams, and commits ahead
→ warn clearly when SSH or synchronization failed
→ ask for confirmation
```

After confirmation, `envr` deletes every conflicting managed instance, waits
until the API confirms absence, removes obsolete SSH host entries, and clears
transient instance state. Repository credentials and Codex authentication
remain cached for the next disposable VPS.

Stopping is not exposed because Vultr continues billing stopped instances.

## Security boundaries

- The Vultr API token is entered independently on each trusted client,
  validated before being saved, never passed to an external process, and
  remains protected by end-to-end TLS through an optional relay.
- API access must be permitted for the client’s public IP range. Broad IPv4
  access may be necessary for mobile networks, so Vultr account 2FA and token
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
→ open a port
→ destroy from Android
→ recreate from a synchronized device
```
