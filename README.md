# envr

`envr` is a small command-line tool for using one disposable Vultr development
VPS from Android Termux. It prepares Git repositories, remote shells, Codex
authentication, development tools, and safe instance cleanup.

Status: implemented and locally tested; one funded-account Vultr validation
cycle remains before calling it production-ready.

## Interface

```bash
envr git
envr up 4
envr up 4 repository-folder
envr status
envr port-open 443
envr destroy
```

One Vultr account manages at most one `envr` VPS. Multiple repositories and
trusted client devices share it. Each client has its own SSH key; the running
VPS is authoritative for repository configuration and Codex authentication.

## Intended workflow

Configure a repository once, on any device:

```bash
envr git
```

This stores the repository configuration and dedicated key locally but does
not contact the Git host. Clone and access verification happen only from the
disposable VPS during `envr up`.

Create or reconnect to the VPS and open a normal shell in the general workspace:

```bash
envr up 4
```

After the first successful connection on a device, `envr up` tries its cached
IP and trusted SSH host key before contacting Vultr. If the VPS is reachable,
an API outage does not prevent reconnection. Stale or untrusted cached
connections fail closed and fall back to live Vultr discovery.

The normal prompt is short:

```text
remote@envr:~/envr$
```

Select a configured repository when needed:

```bash
envr up 4 repository-folder
```

`envr` never starts Codex automatically. Inside the remote shell:

```bash
scodex auth
scodex resume
```

`scodex auth` starts device authentication. `scodex resume` resumes Codex with
approvals and sandboxing explicitly bypassed; use it only because this VPS is
disposable and isolated for that purpose.

To join from another Mac, Linux machine, or Android phone, install `envr`, enter
the same Vultr API token, and run the same command. `envr` discovers the
running VPS and registers that device's public key. If Vultr does not expose
the running VPS password, `envr` asks once for its root SSH password. It
installs the new device's public key and immediately reconnects key-only.
Alternatively, run `envr up` once from a device that already has access; it
securely synchronizes all registered envr public keys. On connection, the new
device immediately receives the current repository configuration, dedicated
Git keys, and Codex auth cache from the VPS.

Every successful `up` also establishes one strong recovery password for that
VPS, synchronizes it to the connected device with mode `600`, and durably
records the IP, device key, and trusted SSH host key. An authorized device can
show the recovery password with `envr status`; a new device enters it once,
then all later connections are key-only and can proceed without the Vultr API.

Inspect the Vultr account, instance, live price, IP, and envr-managed recovery
root password:

```bash
envr status
```

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

Each device stores only its API token, device SSH key, and a synchronized cache
of repository credentials and Codex authentication. Repository working trees,
shell tools, builds, and containers remain on the VPS.

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
