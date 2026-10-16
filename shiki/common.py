"""Small security and protocol helpers. No third-party dependencies."""
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlsplit

MAX_BODY = 131072
ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')


def ident(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError('invalid identifier')
    return value


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def new_token():
    return secrets.token_urlsafe(32)


def private_dir(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError('private directory must not be a symlink')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError('private directory must belong to current user')
    path.chmod(0o700)
    return path


def private_file(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('private file must not be a symlink')
    if path.exists():
        if path.stat().st_uid != os.getuid():
            raise ValueError('private file must belong to current user')
        path.chmod(0o600)
    return path


def write_private(path, value):
    path = Path(path)
    private_dir(path.parent)
    private_file(path)
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    fd, name = tempfile.mkstemp(prefix='.shiki-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, limit=MAX_BODY):
    with open(path, 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('configuration too large')
    return json.loads(data)


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def safe_url(value, transport=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('invalid URL')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or any(ord(c) < 33 for c in value)):
        raise ValueError('URL must be credential-free HTTP(S)')
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError('invalid URL port') from exc
    if transport and (parsed.query or parsed.path not in ('', '/') or
                      (parsed.scheme != 'https' and not is_loopback(parsed.hostname))):
        raise ValueError('receiver URL must use HTTPS (HTTP allowed only on loopback) and have no path/query')
    return value.rstrip('/')


def text(value, maxlen=256, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > maxlen or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError('invalid text')
    return value


def number(value, low=0, high=1e18):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('invalid numeric metric')
    return value


def boolean(value):
    if value is None or isinstance(value, bool):
        return value
    raise ValueError('invalid boolean metric')


def obj(value):
    if not isinstance(value, dict):
        raise ValueError('object required')
    return value


def bounded_list(value, limit):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('list limit exceeded')
    return value


def fields(source, specs):
    source = obj(source)
    return {key: fn(source.get(key)) for key, fn in specs.items()}


def normalize_snapshot(raw):
    """Copy only defined telemetry fields; never retain arbitrary agent input."""
    raw = obj(raw)
    hardware = obj(raw.get('hardware', {}))
    result = {'hardware': {}}
    result['hardware']['cpu'] = fields(hardware.get('cpu', {}), {
        'name': text, 'usage_percent': lambda v: number(v, 0, 100),
        'temperature_c': lambda v: number(v, -100, 250), 'error': text})
    result['hardware']['gpus'] = [fields(v, {
        'name': text, 'usage_percent': lambda v: number(v, 0, 100),
        'temperature_c': lambda v: number(v, -100, 250), 'memory_used_bytes': number,
        'memory_total_bytes': number, 'error': text}) for v in bounded_list(hardware.get('gpus'), 16)]
    result['hardware']['memory'] = fields(hardware.get('memory', {}), {
        'used_bytes': number, 'available_bytes': number, 'total_bytes': number, 'error': text})
    result['hardware']['disks'] = [fields(v, {'mount': text, 'used_bytes': number,
        'total_bytes': number, 'error': text}) for v in bounded_list(hardware.get('disks'), 32)]
    services = obj(raw.get('services', {}))
    result['services'] = {}
    for name in ('codex', 'docker', 'tailscale', 'sunshine', 'loki', 'grafana', 'updates'):
        item = fields(services.get(name, {}), {'status': text, 'checked_at': number, 'detail': text})
        if item['status'] not in ('ok', 'error', 'unknown', 'not_installed'):
            item['status'] = 'unknown'
        result['services'][name] = item
    docker = obj(raw.get('docker', {}))
    result['docker'] = fields(docker, {'status': text, 'total': number, 'running': number, 'exited': number, 'error': text})
    result['docker']['containers'] = [fields(v, {'id': text, 'name': text, 'state': text,
        'health': text, 'cpu_percent': lambda v: number(v, 0, 100000), 'memory_bytes': number,
        'error': text}) for v in bounded_list(docker.get('containers'), 128)]
    tail = obj(raw.get('tailscale', {}))
    result['tailscale'] = fields(tail, {'status': text, 'online_peers': number, 'error': text})
    result['tailscale']['peers'] = [fields(v, {'name': text, 'online': boolean})
        for v in bounded_list(tail.get('peers'), 128)]
    result['codex'] = fields(raw.get('codex', {}), {'gui_running': boolean, 'cli_running': boolean,
        'model': text, 'task_state': text, 'last_task_at': number, 'last_task_label': text,
        'realtime_tps': number, 'average_tps': number, 'error': text})
    updates = obj(raw.get('updates', {}))
    result['updates'] = {name: fields(updates.get(name, {}), {'state': text, 'checked_at': number,
        'last_success_at': number, 'version': text, 'detail': text}) for name in ('system', 'cli', 'gui')}
    result['updates']['reboot_required'] = boolean(updates.get('reboot_required'))
    result['delivery'] = [fields(v, {'receiver_id': lambda v: ident(v), 'status': text,
        'last_success_at': number, 'error': text}) for v in bounded_list(raw.get('delivery'), 16)]
    return result
