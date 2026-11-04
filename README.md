# bulk_command

Run a shell command in every **running** Proxmox LXC container, optionally
filtered by container tags.

**Requires Proxmox 8.0+** (for the LXC status and interfaces APIs) and SSH
access to each container (containers run their own sshd).

## Cluster config file

Host, user, and credentials live in a JSON config file so you don't type
them every run.

- **Default path:** `~/.config/bulk_command/config.json`
- **Override:** `--config PATH` or the `BULK_COMMAND_CONFIG` env var

See [`config.example.json`](config.example.json) for a worked example.

Format:

```json
{
  "default_cluster": "home-lab",
  "clusters": {
    "home-lab": {
      "host": "pve1",
      "port": 8006,
      "user": "root@pam",
      "token_name": "bulkcommand",
      "token_value": "abc123secret",
      "ssh_user": "root",
      "ssh_key": "~/.ssh/id_ed25519"
    },
    "work": {
      "host": "pve9",
      "user": "admin@pve!key",
      "token_value": "xyz789",
      "timeout": 30
    }
  }
}
```

`default_cluster` names the cluster used when you pass no `--cluster` and no
`--host`/`--user`/`--token-value`/`--password` flags. Each cluster entry
supports: `host`, `port`, `user`, `token_name`, `token_value`, `password`,
`insecure`, `timeout`, `ssh_user`, `ssh_key`.

**Precedence per field:** CLI flag > named cluster (`--cluster NAME`) >
`default_cluster` > built-in default (`port=8006`, `ssh_user` = your
username, key auth via your standard `~/.ssh` keys).

> Store the file with `chmod 600` — it contains your token secret.

**Self-signed API cert:** Proxmox clusters commonly use a self-signed
certificate, which makes the default SSL verification fail. Set
`"insecure": true` on the cluster (or pass `-k` / `--insecure`) to skip
verification.

## How it works

1. `GET /nodes` — list cluster nodes
2. `GET /nodes/{node}/lxc` — status list; only containers with
   `status == running` that are not templates are considered
3. `GET /nodes/{node}/lxc/{vmid}/config` — read the `tags` field
   (semicolon-delimited string, e.g. `web;prod`)
4. `GET /nodes/{node}/lxc/{vmid}/interfaces` — read the `eth0` IP address
5. `ssh <ssh_user>@<container_ip> '<cmd>'` — run the command directly
   in the container, capturing stdout/stderr with a per-container timeout.
   If the resolved IP is one of the local machine's own addresses
   (i.e. you're running the script from inside that very container),
   the command runs locally instead of over SSH.

**Output convention:** by default, stdout carries only the per-container
header line (`=== name (vmid=...) ===`) and the command output — nothing
else, so `bulk_command.py ... | grep foo` stays clean. Diagnostic
messages (`[info]`, `[scan]`, `[warn]`, `[ok]`, `[fail]`) are suppressed
unless `--verbose` / `-v` is passed; `[error]` messages are always shown
on stderr.

Exit code: `0` if every container succeeded, `1` if any failed, `2` on
connection/authentication errors.

## Usage

Once the config file exists, the common case needs no cluster flags:

```bash
# Run a command in every running container (uses default_cluster)
python3 bulk_command.py 'hostname'

# Use a specific cluster from the config
python3 bulk_command.py --cluster work 'df -h'

# Filter by tags (any-of: container must carry at least one of the tags)
python3 bulk_command.py --tag web --tag prod \
    'systemctl list-timers --no-legend'

# Restrict to specific vmids
python3 bulk_command.py --container 101 --container 105 'df -h /'

# Inspect which containers carry which tags (no command executed)
python3 bulk_command.py --list-tags

# Run as a non-root user inside the containers
python3 bulk_command.py --ssh-user deploy 'whoami'

# One-off override without touching the config file
python3 bulk_command.py --host other-pve --user 'root@pam' \
    --token-value SECRET 'id'
```

## Options

| Option | Description |
|---|---|
| `cmd` | Shell command, run inside each container via its shell |
| `--config` | Path to the JSON config file (default `~/.config/bulk_command/config.json`) |
| `--cluster` | Name of a cluster defined in the config (overrides `default_cluster`) |
| `--host` | Proxmox cluster hostname or IP (overrides config) |
| `--user` | PVE user, e.g. `root@pam` or `root@pam!keyname` (overrides config) |
| `--token-value` | API token secret (overrides config) |
| `--token-name` | API token key name (only needed if not in `--user`) |
| `--password` | Password auth (used when no token given) |
| `--port` | API port (default `8006`) |
| `--insecure` / `-k` | Skip SSL certificate verification |
| `--tag` | Tag filter, repeatable; container matches if it has at least one of the given tags |
| `--container` | vmid filter, repeatable |
| `--timeout` | Per-container command timeout in seconds (default `60`) |
| `--ssh-user` | SSH user in the containers (default: your username) |
| `--ssh-key` | SSH private key path for container logins (default: your standard `~/.ssh` keys) |
| `--verbose` / `-v` | Show diagnostic messages on stderr (`[info]`, `[scan]`, `[warn]`, `[ok]`, `[fail]`) |
| `--list-tags` | Print each running container's tags and exit |

## Notes

- The container must already be **running** — the script does not start
  containers.
- The PVE API user needs read permission on the target LXC vmids (to list
  them and read their tags), and your SSH key (or `ssh_user` login) must be
  authorized on every container's sshd.
- A non-zero exit code from the in-container command is reported as
  `[fail]` per container and reflected in the script's exit code — the SSH
  exit status is the command's own exit status.

## License

MIT
