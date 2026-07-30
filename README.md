# envr

`envr` is a small command-line tool for using one disposable Vultr development
VPS from Android Termux. It prepares Git repositories, persistent tmux
sessions, Codex authentication, development tools, and safe instance cleanup.

Status: implemented and locally tested; one funded-account Vultr validation
cycle remains before calling it production-ready.

## Interface

```bash
envr git
envr up 4 repository-folder
envr status
envr port-open 443
envr destroy
```

One `envr` installation manages at most one VPS. Multiple repositories share
that machine and receive separate Git identities, SSH keys, directories, and
tmux sessions.

## Intended workflow

Configure each repository once:

```bash
envr git
```

Create or reconnect to the VPS and enter that repository:

```bash
envr up 4 repository-folder
```

After an Android or network disconnection, run the same command to reattach to
the repository's persistent Codex session.

Inspect the Vultr account, instance, live price, IP, and generated fallback
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

On Termux, the installer supplies Python, Git, and OpenSSH when needed and
places `envr` in `$PREFIX/bin`. Linux and macOS use `~/.local/bin`.

## Implementation

The client uses Python 3.10 or newer with the standard library only. A
small POSIX installer handles local Python, Git, and OpenSSH prerequisites.
Termux is the primary client; Linux and macOS are secondary supported Unix
clients.

The complete behavior, security model, failure handling, and implementation
contract are specified in [DESIGN.md](DESIGN.md).

## Security

Runtime credentials and state live under `~/envr` with restrictive filesystem
permissions. They must never be committed or placed in Android shared storage.
The root `.gitignore` explicitly excludes these files in case the source
checkout is itself located at `~/envr`.

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
