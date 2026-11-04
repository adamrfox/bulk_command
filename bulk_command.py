#!/usr/bin/env python3
"""
bulk_command.py — run a shell command in every running Proxmox LXC container,
optionally filtered by container tags.

Based on the Proxmox API auth helpers from /home/midway/patch-tuesday
(app/proxmox_api.py). Discovery uses the Proxmox REST API; the command
itself is run over SSH directly to the container (each LXC has its own
IP address and runs sshd).

How it works:
  1. GET  /nodes                       -> cluster nodes
  2. GET  /nodes/{node}/lxc           -> LXC status list (only 'running',
                                          non-templates)
  3. GET  /nodes/{node}/lxc/{vmid}/config  -> 'tags' field for filtering
  4. GET  /nodes/{node}/lxc/{vmid}/interfaces -> eth0 IP address
  5. ssh <ssh_user>@<container_ip> '<cmd>'
     -> run the command inside the container, capture stdout/stderr
     (if the IP is this machine's own address, the command runs locally)

Diagnostic messages go to stderr; per-container command output goes to stdout,
so `bulk_command.py ... | grep ...` stays clean.

Cluster config:
  Credentials live in a JSON config file (default
  ~/.config/bulk_command/config.json, override with --config or the
  BULK_COMMAND_CONFIG env var). The file names one or more clusters and
  marks one as the default:

      {
        "default_cluster": "home-lab",
        "clusters": {
          "home-lab":  { "host": "pve1", "user": "root@pam",
                          "token_name": "bulkcommand", "token_value": "...",
                          "ssh_user": "root", "ssh_key": "~/.ssh/id_ed25519" },
          "work":      { "host": "pve9", "user": "admin@pve!key",
                          "token_value": "..." }
        }
      }

  Precedence per field: CLI flag > named cluster (--cluster NAME) >
  default_cluster > built-in default.

Auth: API token (token_value, key name via user 'x@realm!key' or token_name)
or password, for discovery. SSH key auth (ssh_key) for command execution
in the containers.
"""

import argparse
import getpass
import json
import re
import os
import subprocess
import sys
import time
import urllib3
import warnings

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# When running with --insecure / "insecure": true (self-signed Proxmox cert),
# urllib3 logs an InsecureRequestWarning to stderr. Suppress it so stdout/stderr
# stay clean. (Only affects the warning, not the actual TLS behavior.)
urllib3.disable_warnings()
warnings.filterwarnings('ignore', category=requests.exceptions.RequestsWarning)

_session = requests.Session()
_session.mount('https://',
               HTTPAdapter(max_retries=Retry(connect=3, read=0, backoff_factor=0.5)))

# (base_url, user) -> (ticket, issued_at). Proxmox tickets are valid ~2h.
_ticket_cache = {}
_TICKET_TTL = 3600


_verbose = False


def log(msg):
    """Diagnostic output — stderr only, keeps stdout clean.

    [error] messages are always shown; [info], [scan], [warn], [ok],
    [fail] messages only appear with --verbose.
    """
    if _verbose or msg.startswith('[error]'):
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Cluster config
# ---------------------------------------------------------------------------

CONFIG_PATH_DEFAULT = os.path.expanduser('~/.config/bulk_command/config.json')

# Built-in defaults used when neither the config file nor a CLI flag supplies
# a value.
BUILTIN_DEFAULTS = {
    'host': None,
    'port': 8006,
    'user': None,
    'token_name': None,
    'token_value': None,
    'password': None,
    'insecure': False,
    'timeout': 60,
    # Run commands as the user invoking the script, not root: the SSH
    # login in each container defaults to that user.
    'ssh_user': getpass.getuser(),
    'ssh_key': None,
}


def find_config_file(explicit_path):
    """Resolve the config file path: --config flag > env var > default."""
    if explicit_path:
        return explicit_path
    if os.environ.get('BULK_COMMAND_CONFIG'):
        return os.environ['BULK_COMMAND_CONFIG']
    return CONFIG_PATH_DEFAULT


def load_config(path):
    """Load and validate the config file. Returns (config_dict, default_cluster_name)."""
    if not os.path.exists(path):
        return {}, None

    try:
        with open(path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f'[error] cannot read config file {path}: {e}')
        sys.exit(2)

    clusters = config.get('clusters')
    if not isinstance(clusters, dict) or not clusters:
        log(f'[error] config file {path} has no "clusters" object')
        sys.exit(2)

    # Validate each cluster entry has a host and user.
    for name, c in clusters.items():
        if not isinstance(c, dict):
            log(f'[error] cluster "{name}" in config must be an object')
            sys.exit(2)
        if not c.get('host') or not c.get('user'):
            log(f'[error] cluster "{name}" needs at least "host" and "user"')
            sys.exit(2)

    default_name = config.get('default_cluster')
    if default_name and default_name not in clusters:
        log(f'[error] default_cluster "{default_name}" not found in clusters')
        sys.exit(2)

    return config, default_name


def resolve_cluster_config(args, config, default_name):
    """Merge config + CLI into a single args object with final values.

    Precedence per field: CLI flag > named cluster > default_cluster > builtin.
    """
    # Pick the cluster entry.
    cluster_entry = {}
    if args.cluster:
        if args.cluster not in config.get('clusters', {}):
            log(f'[error] --cluster "{args.cluster}" not found in config '
                f'(available: {", ".join(config.get("clusters", {}))})')
            sys.exit(2)
        cluster_entry = config['clusters'][args.cluster]
        log(f'[info] using cluster "{args.cluster}" from config')
    elif default_name:
        cluster_entry = config['clusters'][default_name]
        log(f'[info] using default cluster "{default_name}"')
    # else: no cluster entry — rely on CLI flags / builtins

    # Merge, highest priority first.
    def pick(cli_value, cfg_value, builtin_value):
        if cli_value is not None:
            return cli_value
        if cfg_value is not None:
            return cfg_value
        return builtin_value

    args.host = pick(args.host, cluster_entry.get('host'), BUILTIN_DEFAULTS['host'])
    args.port = pick(args.port, cluster_entry.get('port'), BUILTIN_DEFAULTS['port'])
    args.user = pick(args.user, cluster_entry.get('user'), BUILTIN_DEFAULTS['user'])
    args.token_name = pick(args.token_name, cluster_entry.get('token_name'),
                           BUILTIN_DEFAULTS['token_name'])
    args.token_value = pick(args.token_value, cluster_entry.get('token_value'),
                            BUILTIN_DEFAULTS['token_value'])
    args.password = pick(args.password, cluster_entry.get('password'),
                         BUILTIN_DEFAULTS['password'])
    # in_secure is a flag (store_true) — only override if config says True.
    if not args.insecure and cluster_entry.get('insecure'):
        args.insecure = True
    args.timeout = pick(args.timeout, cluster_entry.get('timeout'),
                        BUILTIN_DEFAULTS['timeout'])
    args.ssh_user = pick(args.ssh_user, cluster_entry.get('ssh_user'),
                         BUILTIN_DEFAULTS['ssh_user'])
    args.ssh_key = pick(args.ssh_key, cluster_entry.get('ssh_key'),
                        BUILTIN_DEFAULTS['ssh_key'])
    if args.ssh_key:
        args.ssh_key = os.path.expanduser(args.ssh_key)
    # Post-check: need host and user.
    if not args.host:
        log('[error] no cluster host configured — set it in the config file '
            'or pass --host')
        sys.exit(2)
    if not args.user:
        log('[error] no cluster user configured — set it in the config file '
            'or pass --user')
        sys.exit(2)
    # Need one form of auth.
    if not args.token_value and not args.password:
        log('[error] no credentials — set token_value or password in the '
            'config file, or pass --token-value / --password')
        sys.exit(2)


# ---------------------------------------------------------------------------
# Auth helpers (mirrors patch-tuesday/app/proxmox_api.py)
# ---------------------------------------------------------------------------


def _parse_token_user(user_string):
    """Parse 'user@realm!keyname' — also strips bash-escaped \\! artifacts."""
    cleaned = user_string.replace('\\!', '!').replace('\\\\', '\\')
    if '!' in cleaned:
        parts = cleaned.rsplit('!', 1)
        return f'{parts[0]}!{parts[1]}'
    return cleaned


def _build_api_token_header(service, username, token_name, token_value):
    """Build the PVEAPIToken Authorization header value."""
    clean_username = username.replace('\\!', '!').replace('\\\\', '\\')
    clean_token_value = (token_value or '').replace('\\!', '!').replace('\\\\', '\\')
    if token_name and '!' not in clean_username:
        return f'{service}APIToken={clean_username}!{token_name}={clean_token_value}'
    return f'{service}APIToken={clean_username}={clean_token_value}'


def _auth_headers_cookies(args, user, token_name, token_value):
    """Return (headers, cookies) for one API request."""
    headers = {'Accept': 'application/json'}
    cookies = None
    if token_value:
        headers['Authorization'] = _build_api_token_header(
            'PVE', user, token_name, token_value)
    elif args.password:
        cache_key = (args.base_url, user)
        cached = _ticket_cache.get(cache_key)
        if cached and time.time() - cached[1] < _TICKET_TTL:
            pve_ticket = cached[0]
        else:
            r = _session.post(f'{args.base_url}/api2/json/access/ticket',
                              data={'username': user, 'password': args.password},
                              verify=not args.insecure, timeout=10)
            r.raise_for_status()
            ticket_data = r.json().get('data') or {}
            pve_ticket = ticket_data.get('ticket') or ''
            if not pve_ticket:
                raise PermissionError('No ticket returned from /access/ticket')
            _ticket_cache[cache_key] = (pve_ticket, time.time())
        cookies = {'PVEAuthCookie': pve_ticket}
    return headers, cookies


def api_request(args, user, token_name, token_value, method, path,
                params=None, timeout=15):
    """GET/PUT helper. Returns the 'data' field of the JSON response."""
    url = f'{args.base_url}/api2/json{path}'
    headers, cookies = _auth_headers_cookies(args, user, token_name, token_value)
    r = _session.request(method, url, params=params, headers=headers,
                         cookies=cookies, verify=not args.insecure,
                         timeout=timeout)
    if r.status_code == 401:
        _ticket_cache.pop((args.base_url, user), None)
        raise PermissionError(f'Authentication failed for {path}')
    if r.status_code >= 400:
        detail = ''
        try:
            err = r.json()
            detail = err.get('errors') or str(err)
        except Exception:
            detail = r.text
        raise RuntimeError(f'API error {r.status_code} for {path}: {detail}')
    r.raise_for_status()
    return r.json().get('data')


def get_container_ip(args, user, token_name, token_value, node, vmid):
    """Get the container's primary IPv4 address via the Proxmox interfaces
    endpoint. Returns the IP string, or None if not found.
    """
    data = api_request(args, user, token_name, token_value,
                       'GET', f'/nodes/{node}/lxc/{vmid}/interfaces',
                       timeout=10)
    if not isinstance(data, list):
        return None
    for iface in data:
        if iface.get('name') == 'eth0':
            inet = iface.get('inet', '')
            if inet:
                return inet.split('/')[0]
    return None


def local_ipv4_addrs():
    """IPv4 addresses of the local machine's interfaces.

    Used to detect when a target container is this machine itself
    (e.g. running the script from inside one of the cluster's own
    containers): in that case the command runs locally, not over SSH.
    """
    try:
        proc = subprocess.run(['ip', '-4', '-o', 'addr', 'show'],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            addrs = set()
            for line in proc.stdout.splitlines():
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == 'inet' and i + 1 < len(parts):
                        addrs.add(parts[i + 1].split('/')[0])
            return addrs
    except Exception:
        pass
    return set()


def _ssh_args_base(args, host):
    """Full ssh command list: ssh + options + user@host. Key auth if ssh_key is set."""
    a = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=no',
         '-o', 'ConnectTimeout=10', '-p22']
    user = args.ssh_user or 'root'
    if args.ssh_key:
        a.extend(['-i', args.ssh_key])
    a.append(f'{user}@{host}')
    return a


def exec_command(args, user, token_name, token_value, host, vmid, cmd, timeout):
    """Run cmd in LXC vmid: locally if the target IP is this machine's own
    address, otherwise over SSH directly to the container.

    Returns (ok: bool, output: str, status: str).
    """
    if host in local_ipv4_addrs():
        # This machine is the container itself — no SSH hop needed.
        proc = subprocess.run(['sh', '-c', cmd], capture_output=True,
                              text=True, timeout=timeout + 30)
        if proc.returncode == 0:
            return True, proc.stdout, 'ok (local)'
        return False, proc.stdout, f'exit {proc.returncode}: {proc.stderr.strip()}'

    # Pass the command as a single argument; ssh sends it to the remote
    # shell which parses it (quotes, pipes, etc. all work naturally).
    ssh_cmd = _ssh_args_base(args, host) + [cmd]

    try:
        proc = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=timeout + 30)
        if proc.returncode == 0:
            return True, proc.stdout, 'ok'
        return False, proc.stdout, f'exit {proc.returncode}: {proc.stderr.strip()}'
    except subprocess.TimeoutExpired:
        return False, '', f'timeout after {timeout + 30}s'
    except Exception as e:
        return False, '', f'ssh failed: {e}'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(
        description='Run a command in every running Proxmox LXC container, '
                    'optionally filtered by tags.\n\n'
                    'Cluster host/user/credentials come from the config file '
                    '(--config or BULK_COMMAND_CONFIG) unless overridden on the '
                    'command line. See the config section in --help for '
                    'details.')
    p.add_argument('cmd', help='Shell command to run inside each container')
    p.add_argument('--config',
                   help=f'Path to the JSON config file '
                        f'(default: {CONFIG_PATH_DEFAULT}, '
                        f'or $BULK_COMMAND_CONFIG)')
    p.add_argument('--cluster',
                   help='Name of a cluster defined in the config file '
                        '(overrides default_cluster)')
    p.add_argument('--host',
                   help='Proxmox cluster hostname or IP (overrides config)')
    p.add_argument('--user',
                   help='PVE user, e.g. root@pam or root@pam!keyname '
                        '(overrides config)')
    p.add_argument('--token-name',
                   help='API token key name (only needed if not in --user)')
    p.add_argument('--token-value',
                   help='API token secret (long random string)')
    p.add_argument('--password',
                   help='Password auth (used if no --token-value)')
    p.add_argument('--port', type=int,
                   help='API port (default 8006)')
    p.add_argument('--insecure', '-k', action='store_true',
                   help='Skip SSL certificate verification')
    p.add_argument('--tag', action='append', default=[],
                   help='Filter: container must carry at least one of these tags '
                        '(repeatable; no tags = all running containers)')
    p.add_argument('--container', action='append', default=[],
                   help='Restrict to specific vmid(s) (repeatable)')
    p.add_argument('--timeout', type=int,
                   help='Per-container command timeout in seconds (default 60)')
    p.add_argument('--ssh-user',
                   help='SSH user in the containers (default: your username; '
                        'the SSH login user for each container)')
    p.add_argument('--ssh-key',
                   help='SSH private key path (default: your standard ~/.ssh keys)')
    p.add_argument('--list-tags', action='store_true',
                   help='Just print the tags of each running container and exit')
    p.add_argument('--verbose', '-v', action='store_true',
                   help='Show diagnostic messages on stderr '
                        '([info], [scan], [warn], [ok], [fail])')
    return p.parse_args()


def main():
    global _verbose
    args = parse_args()
    _verbose = args.verbose

    # --- load config and resolve cluster config + CLI overrides ---
    config_path = find_config_file(args.config)
    config, default_name = load_config(config_path)
    if config:
        log(f'[info] config: {config_path}')
    resolve_cluster_config(args, config, default_name)

    args.base_url = f'https://{args.host}:{args.port}'
    user = _parse_token_user(args.user)
    token_value = args.token_value
    token_name = args.token_name

    # --- auth test / node list ---
    try:
        nodes = api_request(args, user, token_name, token_value,
                            'GET', '/nodes', timeout=15)
    except Exception as e:
        log(f'[error] cannot reach Proxmox at {args.base_url} as {user}: {e}')
        sys.exit(2)
    if not isinstance(nodes, list) or not nodes:
        log('[error] no nodes returned — check credentials/permissions')
        sys.exit(2)

    log(f'[info] {len(nodes)} node(s): {[n["node"] for n in nodes]}')

    # --- collect running LXC containers ---
    targets = []
    for node_info in nodes:
        node = node_info.get('node')
        try:
            lxc_list = api_request(args, user, token_name, token_value,
                                  'GET', f'/nodes/{node}/lxc', timeout=15)
        except Exception as e:
            log(f'[warn] node {node}: {e}')
            continue
        for entry in lxc_list if isinstance(lxc_list, list) else []:
            if entry.get('type') != 'lxc' or entry.get('template') == 1:
                continue
            if entry.get('status') != 'running':
                continue
            vmid = str(entry.get('vmid'))
            name = entry.get('name', f'unknown-{vmid}')
            if args.container and vmid not in set(args.container):
                continue
            log(f'[scan] {name} (vmid={vmid}) on {node}')
            tags = set()
            try:
                config = api_request(args, user, token_name, token_value,
                                    'GET', f'/nodes/{node}/lxc/{vmid}/config',
                                    timeout=15)
                if isinstance(config, dict):
                    tags = {t.strip() for t in re.split(r'[,;]', config.get('tags') or '') if t.strip()}
            except Exception as e:
                log(f'[warn] {name} ({vmid}): cannot read tags: {e}')
            targets.append({'node': node, 'vmid': vmid, 'name': name,
                           'tags': tags})

    # --- tag filter (any-of semantics: container matches if it carries at
    #     least one of the requested tags) ---
    if args.tag:
        wanted = set(args.tag)
        targets = [t for t in targets if t['tags'] & wanted]

    if not targets:
        log(f'[info] no containers matched'
            + (f' tags={args.tag}' if args.tag else ' (running, non-template)'))
        sys.exit(0)

    if args.list_tags:
        for t in targets:
            print(f'{t["name"]} ({t["vmid"]}): {", ".join(sorted(t["tags"])) or "(none)"}')
        sys.exit(0)

    # --- resolve container IPs via the Proxmox interfaces endpoint ---
    for t in targets:
        try:
            t['ip'] = get_container_ip(args, user, token_name, token_value,
                                     t['node'], t['vmid'])
        except Exception as e:
            t['ip'] = None
            log(f'[warn] {t["name"]} ({t["vmid"]}): cannot read interfaces: {e}')
        if not t['ip']:
            log(f'[warn] {t["name"]} ({t["vmid"]}): no IP address found')
    if any(t['ip'] is None for t in targets):
        bad = [t['name'] for t in targets if t['ip'] is None]
        log(f'[error] {len(bad)}/{len(targets)} container(s) have no IP address '
            f'({", ".join(bad)}) — nothing to run against')
        sys.exit(2)

    log(f'[info] running {args.cmd!r} as {args.ssh_user} on {len(targets)} container(s) '
        f'(timeout {args.timeout}s)')

    failures = 0
    for t in targets:
        print(f'=== {t["name"]} (vmid={t["vmid"]}) ===', file=sys.stdout)
        ok, output, status = exec_command(
            args, user, token_name, token_value, t['ip'], t['vmid'],
            args.cmd, args.timeout)
        if output:
            # Write the command output as-is, ensuring it ends with a
            # newline so the next header starts on a fresh line.
            if not output.endswith('\n'):
                output += '\n'
            sys.stdout.write(output)
        sys.stdout.flush()  # appear immediately, even when piped
        if ok:
            log(f'[ok] {t["name"]} ({t["vmid"]})')
        else:
            failures += 1
            log(f'[fail] {t["name"]} ({t["vmid"]}): {status}')

    log(f'[info] done: {len(targets) - failures}/{len(targets)} succeeded')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
