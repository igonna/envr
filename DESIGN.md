# envr working design

Status: requirements finalized; implementation and live validation pending.

## Implementation choice

The implementation is Python 3.10 or newer, using the standard library only.
It is not a large Bash script and does not use an application framework.

Python owns:

```text
argument parsing
JSON state and API responses
Vultr HTTPS requests
secret generation
atomic files and permissions
locking
retry/time-out logic
cloud-init generation
status formatting
subprocess orchestration
```

External `ssh`, `scp`, `ssh-keygen`, and `git` commands are still used for the
operations they implement best. Remote Ubuntu bootstrap fragments may use
Bash, but the local `envr` program does not depend on Bash-specific behavior.

The implementation uses standard-library modules such as:

```text
argparse
getpass
hashlib
json
os
pathlib
secrets
subprocess
tempfile
time
urllib.request
```

No `requests`, Click, PyYAML, package environment, daemon, database, or
background service is required.

Reasons for not using Bash as the main implementation:

- macOS ships an older Bash than current Linux and Termux environments;
- GNU and BSD command-line utilities differ;
- nested JSON, retries, atomic state, and error classification are fragile in
  shell;
- secrets are easier to accidentally expose through command arguments;
- Python permits focused unit tests for API and state behavior.

A small POSIX `install.sh` is the only bootstrap shell code. It detects the
local platform, ensures Python 3 plus Git/OpenSSH are present, installs the
Python executable as `envr`, and exits. The daily interface remains only the
documented `envr` commands.

Primary supported client:

```text
Android Termux
```

Secondary Unix clients:

```text
Linux
macOS
```

Package bootstrapping is isolated to the installer and a small dependency
adapter:

```text
Termux          pkg
Debian/Ubuntu   apt-get
macOS           existing tools or Homebrew when available
```

If a supported package manager is unavailable, installation stops with one
clear prerequisite message rather than modifying the system through an
unknown installer.

State files use a small explicit schema version. Writes use a temporary file,
`fsync`, permission setting, and atomic replacement. Unknown or malformed
state stops with a clear error instead of being guessed or silently replaced.

The standard-library `unittest` suite covers argument validation, repository
URL/folder derivation, API error classification, region fallback, duplicate
instance reconciliation, atomic state, password generation, auth
synchronization decisions, Git safety summaries, and firewall commands.
Provider calls and external processes are faked in unit tests. Release
validation additionally includes:

```text
installer test on Termux
Python tests on Linux and macOS
one real low-cost Vultr create/connect/status/port-open/destroy cycle
```

## Goal

`envr` gives a Termux user a disposable Vultr development machine with Codex,
Git, SSH, and persistent tmux sessions already prepared.

The normal interface has five commands:

```bash
envr git
envr up 4 envr
envr status
envr port-open 443
envr destroy
```

`envr up` means "make the environment available and connect to it." If the
managed VPS already exists, it must reconnect rather than create another VPS.
The RAM and repository-folder arguments are always required.

## Single-instance model

One Termux `envr` installation manages exactly zero or one Vultr VPS.

Multiple repositories do not create multiple machines. They live together on
the single VPS, each with its own directory, Git identity, SSH key, and tmux
session:

```text
one Termux envr
→ one Vultr VPS
   ├── repository A / tmux session A
   ├── repository B / tmux session B
   └── repository C / tmux session C
```

Consequences:

- `envr up 4 repo-a` creates the VPS only when none exists.
- `envr up 4 repo-b` reuses the same VPS and opens repository B.
- Reconnecting to any repository never creates another VPS.
- `envr status`, `envr port-open`, and `envr destroy` always target that one
  VPS and need no machine selector.
- To change RAM, destroy the existing VPS and create the next one with the new
  size.

Creation uses a local lock, an immediately saved instance ID, and a unique
provider-side management tag to prevent duplicates across concurrent,
interrupted, or ambiguous API requests.

Finding more than one instance with envr's unique management tag is an error,
not a supported operating mode. In that exceptional case:

```text
envr status     → lists every conflicting instance and marks the conflict
envr up         → refuses to create or connect
envr port-open  → refuses to choose a target
envr destroy    → checks each instance and offers to destroy all conflicting
                  envr-managed instances
```

No command silently guesses which duplicate instance is correct, and no
tagged duplicate is silently left running and billing.

## Before the first command

The user performs the Vultr website setup once:

```text
create/verify account
→ add payment method or credit
→ create API token
→ permit API access from the changing Android address
```

After placing the project in Termux, the canonical installation is:

```bash
sh install.sh
```

No separate `envr init` command exists.

The installer provides the Python runtime needed to start `envr`. Every
`envr` command still checks the external programs it needs. When a required
client package is absent and a supported package manager is available, it
installs that package and continues. A package-installation failure stops
before any Vultr resource is created or deleted.

## User-provided inputs

Required:

```text
~/envr/api
```

Optional:

```text
~/envr/codex/auth.json
```

The Vultr API token is requested without echo by the first command that needs
the Vultr API if the file does not exist. Codex authentication is obtained
with device login when no valid cached file exists.

Repositories are configured through `envr git`. Each repository has its own
SSH identity and Git author identity. This allows different repositories to
belong to different Git users without sharing or confusing their keys. The
user's primary SSH key does not need to be copied to a VPS.

## Local private state

```text
~/envr/
├── api
├── codex/
│   └── auth.json
├── git/
│   ├── igonna-envr/
│   │   ├── id_ed25519
│   │   └── id_ed25519.pub
│   └── another-user-project/
│       ├── id_ed25519
│       └── id_ed25519.pub
├── ssh/
│   ├── id_ed25519
│   ├── id_ed25519.pub
│   └── known_hosts
├── instance-password
├── repos.json
└── state.json
```

Directories use mode `700`. Tokens, private keys, authentication files, and
state files use mode `600`. None of these files belongs in Android shared
storage, Git, messaging applications, email, or cloud notes.

`repos.json` records repository SSH URLs, derived folder names, local key
paths, and repository-local Git author names/emails. It contains no
credentials.

## `up` command grammar

The exact grammar is:

```text
envr up RAM REPOSITORY_FOLDER
```

`RAM` is mandatory and must be `1`, `2`, `4`, or `8`.

`REPOSITORY_FOLDER` is mandatory and must match a repository previously
configured by `envr git`.

If it is unknown, `envr up` stops with:

```text
Repository "envr" is not configured. Run: envr git
```

Valid:

```bash
envr up 4 envr
envr up 8 another-project
```

Invalid:

```bash
envr up
envr up 4
envr up envr
envr up 4 git@github.com:igonna/envr.git
```

If a managed VPS already exists with a different RAM size, `envr up` does not
resize, replace, or create another server. It reports the mismatch and tells
the user to destroy the existing VPS before choosing another size.

## `envr git`

`envr git` is the only repository-configuration path. It takes no command-line
arguments and interactively asks for:

```text
Repository SSH URL:
SSH private key path (leave empty to generate one):
Git author name:
Git author email:
```

Standard SSH Git URLs are supported:

```text
git@HOST:OWNER/REPOSITORY.git
```

The repository URL is the unique identity of a configuration. The remote
folder is derived from the final repository path component:

```text
git@github.com:igonna/envr.git
→ folder: envr
→ remote path: /home/codex/envr/envr
```

If a different configured URL would produce an already-used folder name,
`envr git` rejects it clearly rather than inventing a surprising name.

If an SSH private-key path is supplied:

```text
validate the private key
→ reject it if it requires a passphrase
→ copy it into the private envr data directory
→ derive/store its public key
→ chmod 600
→ verify repository access
```

The original external key file is not used after configuration, so moving it
later cannot break `envr`.

Repository keys must be unencrypted dedicated keys. A passphrase-protected key
would stop working after the phone disconnects because no persistent local
agent would remain available. Keys generated by `envr` are dedicated,
unencrypted Ed25519 keys protected by filesystem permissions.

If the SSH-key prompt is left empty:

```text
generate a dedicated Ed25519 key
→ show/copy the public key
→ wait for the user to install it
→ verify access with git ls-remote
```

Whether the key was imported or generated, `envr git` prints it clearly:

```text
Add this PUBLIC key to the Git repository or account:

ssh-ed25519 AAAA... envr:OWNER/REPOSITORY

The private key is never printed.
Press Enter after adding the key:
```

`envr` contains no GitHub-, GitLab-, or provider-specific detection, links, or
instructions. The user decides where to add the displayed public key. The
selected private key determines the actual account or repository identity.

Git commit identity is separate from SSH authentication. The remembered author
name and email are written into that repository's local `.git/config`, not a
machine-wide global Git config. This allows repositories on the same VPS to
use different Git-host users and commit identities.

Each configured repository is stored as:

```text
~/envr/git/REPOSITORY_FOLDER/id_ed25519
~/envr/git/REPOSITORY_FOLDER/id_ed25519.pub
```

If the repository URL is already configured, `envr git` identifies the
existing record by URL and prints its current public key again. It then asks
whether to keep it or replace its key or Git identity. Thus `envr git` remains
the only configuration and key-display workflow.

After successful configuration it prints the single canonical launch command:

```text
Saved repository as: envr
Run: envr up 4 envr
```

The repository's private key is copied to the VPS only when that repository is
used. Git operations use its explicit identity file, so SSH cannot
accidentally choose another repository's key.

## Repository placement and safety

Remote repositories live under:

```text
/home/codex/envr/
```

For example:

```text
git@github.com:igonna/envr.git
→ /home/codex/envr/envr
```

For the requested repository folder:

- If its directory does not exist, clone it.
- If it exists, verify that `origin` matches the remembered URL.
- Never automatically run `git pull`, reset files, switch branches, clean the
  working tree, or overwrite an existing directory.
- Start Codex with that repository as its working directory.
- Detect repository-name collisions rather than silently reusing the wrong
  directory.

Each remote repository private key is copied from Termux only when needed and
is installed with mode `600`. The Git host key accepted during the successful
local access check is saved in envr's dedicated `known_hosts` file and reused
on the VPS.

## Remote bootstrap

Cloud-init creates the administrative user `codex`, prepares
`/home/codex/envr`, and installs at least:

```text
git
curl
ca-certificates
build-essential
tmux
jq
rsync
unzip
Node.js
Codex CLI
Docker
ufw
fail2ban
unattended-upgrades
```

The `codex` user has sudo access and permission to use Docker. This is a
disposable development machine, and that user is used consistently for SSH,
repositories, tmux, Git identities, and Codex authentication.

`envr up` does not launch Codex until cloud-init reports successful completion
and the required commands are present. On cloud-init failure, it prints the
failure clearly and applies the bounded failed-deployment cleanup/retry rules;
it never connects to a half-configured environment as though setup succeeded.

## Security and data deletion

This is a disposable Internet-facing development machine, not a high-security
vault. The default hardening is automatic and adds no command or prompt:

```text
SSH key remains the normal envr login
→ root password is random and used only as an external-tool fallback
→ default-deny inbound host firewall
→ allow SSH only at initial creation
→ enable fail2ban for SSH
→ enable unattended security updates
→ do not expose Docker or development ports automatically
```

Installing a VPN or another network service may require that tool or the user
to open its required firewall port. `envr` does not guess or automatically
expose application ports.

The initial firewall is configured exactly once by cloud-init:

```text
deny incoming by default
allow outgoing
allow TCP port 22 for SSH
enable firewall
```

Later `envr up`, `envr status`, and repository changes never reset, replace, or
reconcile firewall rules. This preserves VPN, Docker, and other service
configuration performed during the lifetime of the VPS.

Password authentication and direct root login remain enabled intentionally
because `envr status` exposes a fallback credential for external tools. This is
less secure than key-only SSH. The random password, SSH rate limiting, minimal
open ports, short server lifetime, and prompt destruction reduce that risk but
do not make compromise impossible.

The VPS temporarily contains sensitive material:

```text
selected repository private keys
Codex auth.json
checked-out source code
root password hash
```

Therefore repository keys are dedicated rather than primary personal keys,
Vultr account 2FA should be enabled, the API token must remain private, and
instances should be destroyed promptly after work.

Vultr documents Cloud Compute deletion as permanently erasing the instance
filesystem and making it unrecoverable unless a backup or snapshot exists.
`envr` keeps automatic backups disabled, creates no snapshots, and attaches no
additional storage. After requesting deletion, it waits until the instance is
absent from the Vultr API.

No cloud provider can be treated as an absolute guarantee against provider
failure, malicious insiders, legal retention duties, or an unknown platform
vulnerability. Vultr's published compliance audits provide assurance, but
`envr` cannot independently verify physical-media sanitization. For ordinary
source-code development, provider deletion plus disabled backups is a
reasonable model. Material requiring a stronger forensic-deletion guarantee
needs client-controlled encryption and a separate, more complex design.

Do not run `shred` before deletion: SSDs, thin-provisioned storage, snapshots,
and copy-on-write layers can make it misleading rather than a reliable
physical-erasure guarantee.

## tmux behavior

Each repository gets a separate tmux session. A stable session name is derived
from the repository identity, for example:

```text
codex-igonna-envr
codex-igonna-another-project
```

This provides the following behavior:

- Android can suspend or terminate SSH without terminating Codex.
- Running `envr up` for the same repository reattaches to its existing session.
- Selecting another repository creates or attaches to that repository's
  session without disturbing the first one.
- A normal shell can be opened as another tmux window, so a separate
  `envr shell` command is unnecessary.

## `envr up`

The required RAM value is `1`, `2`, `4`, or `8`.

High-level behavior:

```text
install missing local client dependencies
→ create and secure local state
→ validate RAM and repository configuration locally
→ request the Vultr token if missing
→ generate/upload the Vultr SSH public key if needed
→ load the requested repository configuration and SSH key
→ find the managed Vultr instance
```

All local input and repository checks happen before a billable creation
request.

If an instance exists:

```text
resolve its current IP
→ synchronize a changed remote Codex auth file back to Termux
→ ensure the selected repository exists, cloning only when absent
→ attach to that repository's tmux/Codex session
```

If no instance exists:

```text
query current Vultr plans, OS images, regions, and availability
→ create one instance with regional fallback
→ wait for networking, SSH, and successful cloud-init
→ install the repository key and cached Codex auth, when present
→ clone the selected repository if absent
→ attach to that repository's tmux/Codex session
```

After the SSH connection ends, compare Codex auth hashes and atomically pull a
changed remote file. If Android kills the local process first, the next
`envr up` or `envr destroy` performs the synchronization.

All lifecycle-changing operations use a local lock. An instance ID is saved
immediately after creation, and the instance also carries an envr management
tag. If Termux is interrupted during creation, the next command reconciles the
saved ID and tag before attempting anything else. An ambiguous creation
response is followed by an instance lookup before any retry, preventing an
accidental second billable VPS.

## Codex authentication

The remote user is consistently `codex`, so the remote credential path is:

```text
/home/codex/.codex/auth.json
```

Codex is configured to use file-based credential storage. When no valid cache
exists, the persistent tmux session runs:

```bash
codex login --device-auth
```

Credential upload and download are atomic. The receiving side writes a
temporary file, checks that it is nonempty and valid JSON, applies mode `600`,
and renames it into place.

When a VPS exists, its remote auth file is authoritative. When creating a new
VPS, the Termux cache is authoritative.

## `envr status`

The account summary appears first:

```text
Vultr
  Balance:          $18.42
  Pending charges:  $1.58
  Credit runway:    about 26 days at $0.027/hour
```

Then show the managed instance:

```text
envr-codex
  ID:       ...
  Region:   Frankfurt
  RAM:      4 GB
  CPU:      2
  IP:       ...
  SSH user: root
  Password: ...
  Status:   running
  Price:    $0.027/hour · $20/month
```

Pricing comes from the current Vultr plan catalog. Credit runway is only an
estimate based on prepaid credit and current compute price; it cannot measure
the spending capacity of a linked payment card and excludes taxes and
overages.

Before each VPS creation, `envr` generates a new cryptographically random
16-character alphanumeric root password. Alphanumeric output is used so it can
be copied into other applications without shell-quoting surprises. The
plaintext is stored only in:

```text
~/envr/instance-password
```

with mode `600`. Cloud-init preserves root password SSH access. After
cloud-init succeeds, `envr` connects with its SSH key and sends the password
through SSH standard input to `chpasswd`. The plaintext is never placed in
cloud-init, a command argument, or a log. An interrupted setup safely repeats
this step on the next `envr up`.

`envr status` deliberately prints this locally stored password beneath the IP
for use by external administration tools. When the local password is missing,
show:

```text
Password: unavailable locally
```

The normal `envr` connection continues to use the dedicated SSH key and the
`codex` user. Password access is an explicit fallback. Cloud-init must preserve
working password SSH access for `root` so the displayed credential is actually
usable.

`envr status` remains fast and API-only. It does not connect over SSH merely to
inspect repositories or tmux sessions.

When no managed instance exists, it shows the account summary followed by:

```text
envr-codex: not running
```

## `envr port-open`

The exact grammar is:

```text
envr port-open PORT
```

For example:

```bash
envr port-open 443
envr port-open 51820
```

`PORT` must be one integer from `1` through `65535`. To keep the interface
singular and provider-independent, the command opens that port for both TCP
and UDP in the VPS host firewall.

High-level behavior:

```text
validate the port locally
→ locate the exact managed instance through the Vultr API
→ resolve its current IP
→ connect with the dedicated VPS SSH key
→ add an idempotent firewall allow rule for TCP and UDP
→ verify the rules are present
→ print success
```

Example output:

```text
Opened port 443 (TCP and UDP) on envr-codex.
```

Running the same command again succeeds without creating harmful duplicate
rules. If no managed VPS exists, it stops with:

```text
envr-codex is not running. Start it with envr up RAM REPOSITORY_FOLDER.
```

`envr port-open` changes only the VPS host firewall. The initial design does
not attach a restrictive Vultr Firewall group, because doing so would require
duplicating every port rule in two places. A service still must be installed
and listening on the opened port before it is reachable.

## `envr destroy`

High-level behavior:

```text
locate the exact managed instance
→ atomically preserve a changed remote Codex auth file
→ quickly check every repository on the VPS
→ print a compact repository-safety summary
→ ask for confirmation
→ delete the instance
→ wait until deletion is confirmed
→ remove its SSH host-key entry and transient instance state
→ delete the local instance password
```

For every repository directory under `/home/codex/envr`, check:

```text
git status --porcelain
current branch and upstream
commits ahead of upstream
```

Do not run `git pull`, `git fetch`, or modify the repository during this check.
Do not print filenames, diffs, commit messages, or file contents.

Example when everything is safe:

```text
Repository safety:
  envr             clean and pushed
  another-project  clean and pushed

All repositories are committed and pushed.
Destroy VPS? [y/N]
```

Example when work may be lost:

```text
Repository safety:
  envr             UNCOMMITTED
  another-project  2 UNPUSHED COMMITS
  experiment       NO UPSTREAM

Some work may be permanently lost.
Destroy VPS? [y/N]
```

A clean working tree is not sufficient: committed but unpushed commits must be
reported. A branch without an upstream and a detached `HEAD` are also reported
as requiring attention.

If the VPS cannot be reached, `envr` says that repository safety could not be
checked and asks explicitly whether destruction should continue.

If Codex authentication cannot be preserved, ask whether to proceed without
the backup. Never silently destroy first.

The repository keys remain safely cached in Termux and do not need to be copied
back from the VPS.

When no managed instance exists, `envr destroy` prints `envr-codex: not
running` and exits successfully.

## Complete first-use and daily flow

Starting with only the installed `envr` script:

```bash
envr git
```

```text
install missing local Git/SSH dependencies
→ ask for repository URL, key, author name, and author email
→ generate or import the dedicated repository key
→ print its public key
→ wait for the user to add it to the Git host
→ verify access
→ save the repository as its derived folder name
→ print the canonical envr up command
```

Then:

```bash
envr up 4 envr
```

```text
install any remaining local dependencies
→ ask for and save the Vultr API token if missing
→ create local VPS SSH identity if missing
→ discover current Vultr plan/OS/region data
→ create and bootstrap the VPS
→ copy the selected repository key
→ restore cached Codex authentication when present
→ clone the repository only when absent
→ enter its directory
→ create or attach its tmux session
→ run Codex
```

On the first Codex use, the tmux session presents device authentication. Later
VPS instances reuse the cached authentication file.

After an Android or network disconnection:

```bash
envr up 4 envr
```

This resolves the existing VPS and reattaches to the existing repository tmux
session.

To use another repository on the same VPS:

```bash
envr git
envr up 4 another-project
```

The second command copies only that repository's configured key, clones it if
absent, and creates or attaches its separate tmux session. Existing repository
sessions continue running.

To inspect cost and state:

```bash
envr status
```

To expose a service port when needed:

```bash
envr port-open 443
```

To finish:

```text
commit and push work
→ envr destroy
→ preserve changed Codex auth
→ report every repository's uncommitted/unpushed state
→ confirm
→ delete and confirm deletion
```

## Fixed initial choices

```text
Provider:           Vultr
Managed instances:  one maximum
RAM argument:       required; 1, 2, 4, or 8 GB
Regions:            fra, ams, waw
Operating system:   Ubuntu 24.04 LTS x64
Remote user:        codex
Remote repo root:   /home/codex/envr
Backups:            disabled
Extra storage:      none
Public network:     IPv4
```

Plans, prices, OS IDs, region existence, and regional availability are queried
from Vultr rather than trusted as permanent constants.

## Important invariants

- `envr up` never creates a second managed VPS when one already exists.
- One `envr` installation supports exactly one VPS; multiple repositories
  share it.
- Invalid local input is rejected before creating a billable resource.
- Concurrent or interrupted commands are reconciled through a local lock,
  saved instance ID, and provider-side management tag.
- Existing repositories and working trees are never automatically updated or
  overwritten.
- The host firewall is initialized once during cloud-init and is never reset
  by later `envr` commands.
- `envr port-open` changes only the requested port and is idempotent.
- Destruction reports both uncommitted changes and commits not pushed to the
  locally recorded upstream for every managed repository.
- Repositories can only be registered or changed through `envr git`.
- `envr up` accepts a configured folder name, never a Git URL.
- Each repository's Codex process survives SSH disconnections.
- Secrets never appear in command arguments, logs, or debug output.
- The root instance password is the sole intentional exception: `envr status`
  prints it directly to the user's terminal, but never logs or separately
  copies it.
- A failed deployment is either deleted and confirmed deleted or reported
  clearly with its instance ID and billing warning.
- Stopping a Vultr VPS is not part of the interface because stopped instances
  continue to incur charges.
- Destroying the VPS permanently deletes all unpushed repository work.
