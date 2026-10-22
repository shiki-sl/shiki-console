"""Bounded, read-only Linux probes. No commands are accepted from remote input."""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing
import glob
import math
import re
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import time
from urllib.request import build_opener, HTTPRedirectHandler, Request, ProxyHandler
from .common import MAX_BODY, normalize_snapshot, number, safe_url
from .workbench import configured_services, workbench_probe


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def run(argv, timeout=1.5, limit=65536):
    """Fixed-argument local probes, no shell; bound stdout and kill on overflow."""
    import selectors
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    deadline, data = time.monotonic() + timeout, bytearray()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while time.monotonic() < deadline:
            ready = selector.select(min(.1, max(0, deadline - time.monotonic())))
            if ready:
                chunk = os.read(proc.stdout.fileno(), min(8192, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('probe output limit exceeded')
            elif proc.poll() is not None:
                continue
        else:
            raise TimeoutError('probe timed out')
        if proc.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
            raise ValueError('probe unavailable or permission denied')
        return data.decode('utf-8', errors='replace')
    finally:
        selector.close()
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def read_text(path, limit=65536):
    with open(path, 'r', encoding='utf-8', errors='replace') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError('probe input limit exceeded')
    return value.strip()


def processes():
    result = {}
    for path in glob.glob('/proc/[0-9]*/stat')[:32768]:
        try:
            stat = read_text(path, 4096)
            suffix = stat[stat.rfind(')') + 2:].split()
            pid = int(Path(path).parent.name)
            name = stat[stat.find('(') + 1:stat.rfind(')')]
            # Executable basenames and ancestry only; never read command arguments.
            try:
                exe = Path(os.readlink(str(Path(path).parent / 'exe'))).name
            except OSError:
                exe = name
            result[pid] = {'name': name, 'exe': exe, 'ppid': int(suffix[1])}
        except (OSError, ValueError, IndexError):
            continue
    return result


def process_present(procs, name):
    return any(p['exe'].lower() == name or p['name'].lower() == name for p in procs.values())


def cpu_probe():
    def ticks():
        parts = read_text('/proc/stat').splitlines()[0].split()[1:9]
        values = [int(x) for x in parts]
        return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
    result = {'name': None, 'usage_percent': None, 'temperature_c': None, 'error': None}
    try:
        for line in read_text('/proc/cpuinfo', 1048576).splitlines():
            if line.startswith('model name'):
                result['name'] = line.split(':', 1)[1].strip()[:256]
                break
        first = ticks()
        time.sleep(.15)
        second = ticks()
        total = second[0] - first[0]
        result['usage_percent'] = round(max(0, min(100, 100 * (1 - (second[1] - first[1]) / total))), 1) if total > 0 else None
    except (OSError, ValueError, IndexError):
        result['error'] = 'CPU counters unavailable'
    temperatures = []
    for directory in glob.glob('/sys/class/hwmon/hwmon*')[:64]:
        try:
            name = read_text(Path(directory) / 'name', 128)
            if name not in ('k10temp', 'coretemp', 'zenpower'):
                continue
            for path in glob.glob(directory + '/temp*_input')[:32]:
                value = float(read_text(path, 32)) / 1000
                if -100 <= value <= 250:
                    temperatures.append(value)
        except (OSError, ValueError):
            continue
    if temperatures:
        result['temperature_c'] = round(max(temperatures), 1)
    elif result['error'] is None:
        result['error'] = 'CPU temperature sensor unavailable or not readable'
    return result


def memory_probe():
    result = {'used_bytes': None, 'total_bytes': None, 'available_bytes': None, 'error': None}
    try:
        info = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in read_text('/proc/meminfo').splitlines()}
        total, available = info['MemTotal'], info['MemAvailable']
        result.update(total_bytes=total, available_bytes=available, used_bytes=total - available)
    except (OSError, ValueError, KeyError):
        result['error'] = 'Memory counters unavailable'
    return result


def disk_probe(mounts):
    result = []
    for mount in mounts[:32]:
        item = {'mount': mount, 'used_bytes': None, 'total_bytes': None, 'error': None}
        try:
            usage = shutil.disk_usage(mount)
            item.update(used_bytes=usage.used, total_bytes=usage.total)
        except OSError:
            item['error'] = 'Mount unavailable or not readable'
        result.append(item)
    return result


def gpu_probe():
    result = []
    for card in sorted(glob.glob('/sys/class/drm/card[0-9]*'))[:16]:
        if not Path(card).name[4:].isdigit():
            continue
        device = Path(card) / 'device'
        if not device.exists():
            continue
        try:
            vendor = read_text(device / 'vendor', 32)
        except OSError:
            continue
        if vendor == '0x10de':
            continue  # NVIDIA handled by its read-only utility if available.
        item = {'name': ('AMD' if vendor == '0x1002' else 'Intel' if vendor == '0x8086' else 'GPU') + ' · ' + Path(card).name,
                'usage_percent': None, 'temperature_c': None, 'memory_used_bytes': None,
                'memory_total_bytes': None, 'error': None}
        for key, filename, scale in [('usage_percent', 'gpu_busy_percent', 1), ('memory_used_bytes', 'mem_info_vram_used', 1), ('memory_total_bytes', 'mem_info_vram_total', 1)]:
            try:
                item[key] = float(read_text(device / filename, 64)) / scale
            except (OSError, ValueError):
                pass
        temps = []
        for filename in glob.glob(str(device / 'hwmon/hwmon*/temp1_input'))[:8]:
            try:
                temps.append(float(read_text(filename, 32)) / 1000)
            except (OSError, ValueError):
                pass
        item['temperature_c'] = max(temps) if temps else None
        if any(item[k] is None for k in ('usage_percent', 'temperature_c', 'memory_used_bytes', 'memory_total_bytes')):
            item['error'] = 'Some GPU counters unsupported or not readable'
        result.append(item)
    if shutil.which('nvidia-smi'):
        try:
            raw = run(['nvidia-smi', '--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'], timeout=1.5, limit=8192)
            for line in raw.splitlines()[:16]:
                cells = [c.strip() for c in line.split(',')]
                if len(cells) != 5:
                    continue
                item = {'name': cells[0][:256], 'error': None}
                for key, value, factor in zip(('usage_percent', 'temperature_c', 'memory_used_bytes', 'memory_total_bytes'), cells[1:], (1, 1, 1048576, 1048576)):
                    try:
                        item[key] = float(value) * factor
                    except ValueError:
                        item[key] = None
                        item['error'] = 'Some NVIDIA counters unavailable'
                result.append(item)
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            result.append({'name': 'NVIDIA', 'usage_percent': None, 'temperature_c': None,
                           'memory_used_bytes': None, 'memory_total_bytes': None,
                           'error': 'NVIDIA metrics unavailable or permission denied'})
    return result[:16]


def service(status='unknown', detail=None):
    return {'status': status, 'checked_at': time.time(), 'detail': detail}


def docker_memory_bytes(value):
    """Docker human-readable memory counters use SI and IEC units."""
    match = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*((?:[KMGTPE]i?B|B))\s*', str(value), re.IGNORECASE)
    if not match:
        return None
    unit = match[2].lower()
    power = 'bkmgtpe'.index(unit[0]) if unit[0] != 'b' else 0
    factor = (1024 if 'i' in unit else 1000) ** power
    result = float(match[1]) * factor
    return round(result) if math.isfinite(result) and 0 <= result <= 1e18 else None


def docker_resource_stats(socket, containers):
    running = [item for item in containers if item['state'] == 'running']
    if not running:
        return
    try:
        raw = run(['docker', '--host', 'unix://' + str(socket), 'stats', '--no-stream',
                   '--no-trunc', '--format', '{{json .}}'], timeout=3, limit=65536)
        lines = raw.splitlines()
        if len(lines) > 128:
            raise ValueError('stats inventory limit exceeded')
        stats = [json.loads(line) for line in lines]
        if not all(isinstance(row, dict) for row in stats):
            raise ValueError('invalid stats record')
        for item in running:
            found = next((row for row in stats if str(row.get('ID') or row.get('Container') or '') == item['id']
                          or row.get('Name') == item['name']), None)
            if not found:
                continue
            try:
                cpu = float(str(found.get('CPUPerc', '')).strip().rstrip('%'))
                item['cpu_percent'] = cpu if math.isfinite(cpu) and 0 <= cpu <= 100000 else None
            except ValueError:
                pass
            item['memory_bytes'] = docker_memory_bytes(str(found.get('MemUsage', '')).split('/')[0])
            item['error'] = None if item['cpu_percent'] is not None and item['memory_bytes'] is not None else 'Some container resource counters unavailable'
    except (OSError, ValueError, TypeError, TimeoutError, subprocess.TimeoutExpired):
        for item in running:
            item['error'] = 'Container resource sampling unavailable, timed out or permission denied'


def docker_snapshot_probe(path, max_age=60):
    """Consume an explicit root-owned, non-writable snapshot; no elevation."""
    data = {'status': 'unknown', 'total': None, 'running': None, 'exited': None,
            'containers': [], 'error': 'Trusted Docker snapshot unavailable or invalid'}
    checked_at = None
    try:
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('absolute snapshot path required')
        for parent in path.parents:
            info = parent.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
                raise ValueError('unsafe snapshot directory trust chain')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o640
                    or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BODY):
                raise ValueError('unsafe snapshot file')
            raw = source.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError('snapshot exceeds limit')
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or type(snapshot.get('schema')) is not int or snapshot['schema'] != 1:
            raise ValueError('invalid snapshot schema')
        checked_at = number(snapshot.get('sampled_at'))
        if checked_at is None:
            raise ValueError('missing snapshot timestamp')
        normalized = normalize_snapshot({'docker': snapshot.get('docker'),
            'services': {'docker': snapshot.get('service')}})
        data = normalized['docker']
        status = normalized['services']['docker']
        if (data['status'] not in ('ok', 'error', 'unknown') or status['status'] != data['status']
                or status['checked_at'] != checked_at):
            raise ValueError('inconsistent snapshot')
        if not 0 <= time.time() - checked_at <= max_age:
            return ({'status': 'unknown', 'total': None, 'running': None, 'exited': None,
                     'containers': [], 'error': 'Trusted Docker snapshot expired or future-dated'},
                    {'status': 'unknown', 'checked_at': checked_at,
                     'detail': 'Trusted Docker snapshot expired or future-dated; old counters not reused'})
        if data['status'] == 'ok':
            if (data['total'] != len(data['containers']) or data['running'] != sum(item['state'] == 'running' for item in data['containers'])
                    or data['exited'] != sum(item['state'] == 'exited' for item in data['containers'])):
                raise ValueError('inconsistent container counts')
        return data, status
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return ({'status': 'unknown', 'total': None, 'running': None, 'exited': None,
                 'containers': [], 'error': 'Trusted Docker snapshot unavailable or invalid'},
                {'status': 'unknown', 'checked_at': checked_at,
                 'detail': 'Trusted Docker snapshot unavailable or invalid; no privilege escalation attempted'})


def docker_probe(procs, config=None):
    config = config or {}
    if config.get('docker_snapshot_file'):
        return docker_snapshot_probe(config['docker_snapshot_file'], config.get('docker_snapshot_max_age_seconds', 60))
    result = {'status': 'unknown', 'total': None, 'running': None, 'exited': None,
              'containers': [], 'error': None}
    # Never connect to an inactive socket: docker CLI might otherwise activate dockerd.
    if not process_present(procs, 'dockerd'):
        result['error'] = 'No readable running dockerd process; socket left untouched'
        return result, service('unknown', result['error'])
    if not shutil.which('docker'):
        result['error'] = 'Docker client unavailable; daemon process alone does not prove health'
        return result, service('unknown', result['error'])
    try:
        rootless = Path('/run/user') / str(os.getuid()) / 'docker.sock'
        socket = rootless if rootless.exists() else Path('/run/docker.sock')
        rows = run(['docker', '--host', 'unix://' + str(socket), 'ps', '--all', '--no-trunc', '--format', '{{json .}}'], timeout=2, limit=65536)
        if len(rows.splitlines()) > 128:
            raise ValueError('container count exceeds bounded inventory')
        containers = []
        for line in rows.splitlines()[:128]:
            row = json.loads(line)
            status = row.get('Status', '')
            health = 'unhealthy' if '(unhealthy)' in status else 'healthy' if '(healthy)' in status else None
            containers.append({'id': str(row.get('ID', ''))[:64], 'name': str(row.get('Names', ''))[:256],
                'state': str(row.get('State', 'unknown'))[:64], 'health': health,
                'cpu_percent': None, 'memory_bytes': None, 'error': 'Container resources unavailable for this state/sample'})
        docker_resource_stats(socket, containers)
        result.update(status='ok', total=len(containers), running=sum(c['state'] == 'running' for c in containers),
                      exited=sum(c['state'] == 'exited' for c in containers), containers=containers)
        return result, service('ok', 'Docker API responds (native daemon); resources report independent availability')
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        result['error'] = 'Docker API unavailable, timed out or permission denied'
        return result, service('unknown', result['error'])


def tailscale_probe(procs):
    result = {'status': 'unknown', 'online_peers': None, 'peers': [], 'error': None}
    if not process_present(procs, 'tailscaled') or not shutil.which('tailscale'):
        result['error'] = 'Native tailscaled process/client not observable; installation not inferred'
        return result, service('unknown', result['error'])
    try:
        data = json.loads(run(['tailscale', 'status', '--json'], timeout=1.5))
        state = data.get('BackendState')
        result['status'] = 'ok' if state == 'Running' else 'error'
        peers = list(data.get('Peer', {}).values())[:128]
        result['peers'] = [{'name': str(p.get('HostName') or 'peer')[:256], 'online': p.get('Online') if isinstance(p.get('Online'), bool) else None} for p in peers]
        result['online_peers'] = sum(p['online'] is True for p in result['peers'])
        return result, service(result['status'], 'Native Tailscale state: ' + str(state)[:64])
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        result['error'] = 'Native Tailscale API unavailable or permission denied'
        return result, service('unknown', result['error'])


def codex_probe(procs, metadata=False):
    # Desktop ships under both Codex and ChatGPT executable names. GUI descendants
    # include embedded codex app-server and tool hosts, never standalone CLI sessions.
    gui_pids = {pid for pid, p in procs.items() if p['exe'].lower() in ('codex-desktop', 'codex-app', 'codex-electron', 'chatgpt') or p['exe'] == 'Codex'}
    def gui_descendant(pid):
        seen = set()
        for _ in range(64):
            if pid in gui_pids:
                return True
            if pid in seen or pid not in procs:
                return False
            seen.add(pid)
            pid = procs[pid]['ppid']
        return False
    cli = [pid for pid, p in procs.items() if p['exe'].lower() in ('codex', 'codex-cli') and pid not in gui_pids and not gui_descendant(pid)]
    result = {'gui_running': bool(gui_pids), 'cli_running': bool(cli), 'model': None,
        'task_state': 'process_observed' if gui_pids or cli else 'not_observed',
        'last_task_at': None, 'last_task_label': None, 'realtime_tps': None, 'average_tps': None,
        'error': 'Native process observation only; task success and token rates are not inferred'}
    if metadata:
        # Only fixed read-only columns, never thread titles/prompts/output or rollout bodies.
        directory = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        dbs = sorted(directory.glob('state_*.sqlite'))[:4]
        for path in reversed(dbs):
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                    db.execute('PRAGMA query_only=ON')
                    columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
                    if 'updated_at' in columns:
                        model_column = 'model' if 'model' in columns else None
                        selected = 'updated_at,model' if model_column else 'updated_at'
                        row = db.execute('SELECT ' + selected + ' FROM threads ORDER BY updated_at DESC LIMIT 1').fetchone()
                        if row:
                            model = str(row[1])[:256] if model_column and row[1] else None
                            result.update(model=model, last_task_at=float(row[0]),
                                last_task_label='Last observed local model metadata' if model else 'Last observed local thread metadata', task_state='last_observed')
                            result['error'] = 'Model is last observed, not live task identity; token rates unknown' if model else 'Exact model and token rates unknown'
                            break
            except (OSError, ValueError, sqlite3.Error):
                continue
    return result, service('ok' if gui_pids or cli else 'unknown', 'Native GUI/CLI process presence only; no task completion claim')


def health_probe(url):
    try:
        safe_url(url)
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, method='GET', headers={'Accept': 'application/json'}), timeout=1.5) as response:
            response.read(1024)
            return service('ok' if response.status == 200 else 'error', 'Configured health endpoint responds')
    except Exception:
        return service('unknown', 'Configured health endpoint unavailable or TLS validation failed')


DEFAULT_UPDATE_STATUS_FILES = {
    'system': '/var/lib/official-package-update/state.json',
    'cli': '~/.local/state/codex-cli-update/status.json',
    'gui': '~/.local/state/codex-gui-update/status.json',
}


def update_timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        except (ValueError, TypeError):
            return None
    return result if math.isfinite(result) and result >= 0 else None


def normalized_update(item, max_age, now, native=False):
    checked = None
    keys = ('finished_at', 'finished', 'last_finished_at', 'last_attempt', 'last_attempt_at') if native else ('checked_at',)
    for key in keys:
        checked = update_timestamp(item.get(key))
        if checked is not None:
            break
    source_state = item.get('result', item.get('status')) if native else item.get('state')
    state = {'success': 'ok', 'failed': 'error', 'failure': 'error'}.get(source_state, source_state)
    if state not in ('ok', 'error', 'deferred', 'unknown'):
        state = 'unknown'
    success = update_timestamp(item.get('last_success', item.get('last_success_at')))
    version = str(item['version'])[:256] if item.get('version') is not None else None
    detail = str(item.get('error') or item.get('detail') or 'Native updater result; no update command executed')[:256]
    if checked is None or not 0 <= now - checked <= max_age:
        state = 'unknown'
        detail = 'Updater result expired or has no valid check timestamp; last recorded outcome: ' + str(source_state)[:64]
    return {'state': state, 'checked_at': checked, 'last_success_at': success, 'version': version, 'detail': detail}


def updates_probe(config):
    now = time.time()
    max_age = config.get('update_fresh_seconds', 172800)
    paths = DEFAULT_UPDATE_STATUS_FILES | config.get('update_status_files', {})
    result = {}
    for name in ('system', 'cli', 'gui'):
        result[name] = {'state': 'unknown', 'checked_at': None, 'last_success_at': None,
            'version': None, 'detail': 'Native updater state unavailable or permission denied; no update command executed'}
        path = paths.get(name)
        if not path:
            continue
        try:
            source = json.loads(read_text(Path(path).expanduser(), 16384))
            if not isinstance(source, dict):
                raise ValueError('invalid updater record')
            result[name] = normalized_update(source, max_age, now, native=True)
        except (OSError, ValueError, TypeError):
            continue
    result['reboot_required'] = None
    path = config.get('update_metadata_file')
    if path:
        try:
            # Optional explicit artifact overrides only the records it contains.
            source = json.loads(read_text(Path(path).expanduser(), 16384))
            for name in ('system', 'cli', 'gui'):
                if isinstance(source.get(name), dict):
                    result[name] = normalized_update(source[name], max_age, now)
        except (OSError, ValueError, TypeError):
            pass
    statuses = [result[n]['state'] for n in ('system', 'cli', 'gui')]
    status = 'error' if 'error' in statuses else 'ok' if all(x in ('ok', 'deferred') for x in statuses) else 'unknown'
    checked = [result[n]['checked_at'] for n in ('system', 'cli', 'gui')]
    summary = {'status': status, 'checked_at': min(checked) if all(v is not None for v in checked) else None,
        'detail': 'Deferred updates are not failures; timestamps come from updater records'}
    return result, summary


def collect(config):
    """Independent bounded probes with a collection deadline; unavailable is null."""
    now = time.time()
    procs = processes()
    snapshot = normalize_snapshot({})
    timeout = max(2, min(30, float(config.get('collection_timeout_seconds', 8))))
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='probe')
    probes = {'cpu': lambda: cpu_probe(), 'memory': memory_probe,
        'disks': lambda: disk_probe(config.get('disks', ['/'])), 'gpus': gpu_probe,
        'docker': lambda: docker_probe(procs, config), 'tailscale': lambda: tailscale_probe(procs),
        'codex': lambda: codex_probe(procs, config.get('codex_metadata', False)),
        'updates': lambda: updates_probe(config)}
    for name, url in config.get('known_health_urls', {}).items():
        if name in ('loki', 'grafana'):
            probes[name] = lambda url=url: health_probe(url)
    for name in configured_services(config):
        snapshot['services'][name] = service('unknown', '尚未完成本机服务检查')
        probes[name] = lambda name=name: workbench_probe(name)
    futures = {executor.submit(fn): name for name, fn in probes.items()}
    done, pending = wait(futures, timeout=timeout)
    for future in done:
        name = futures[future]
        try:
            value = future.result()
            if name in ('cpu', 'memory', 'disks', 'gpus'):
                snapshot['hardware'][name] = value
            elif name in ('docker', 'tailscale', 'codex', 'updates'):
                snapshot[name], snapshot['services'][name] = value
            else:
                snapshot['services'][name] = value
        except Exception:
            if name in snapshot['services']:
                snapshot['services'][name] = service('unknown', 'Probe failed; unsupported or permission denied')
    for future in pending:
        future.cancel()
        name = futures[future]
        if name in snapshot['services']:
            snapshot['services'][name] = service('unknown', 'Collection deadline exceeded')
    executor.shutdown(wait=False, cancel_futures=True)
    for name in ('grafana', 'loki'):
        if name not in probes:
            snapshot['services'][name] = service('unknown', 'No configured health URL; no scan performed')
    snapshot['services']['sunshine'] = service('ok' if process_present(procs, 'sunshine') else 'unknown',
        'Native Sunshine process observed; streaming acceptance unverified' if process_present(procs, 'sunshine') else 'Native Sunshine not observed; installation/container deployment not inferred')
    # Apply normalization again to guard unsupported sensor values and external metadata.
    try:
        return normalize_snapshot(snapshot)
    except ValueError:
        # A malformed external update record must not discard unrelated hardware telemetry.
        snapshot['updates'] = normalize_snapshot({})['updates']
        snapshot['services']['updates'] = service('unknown', 'Invalid configured updater metadata')
        return normalize_snapshot(snapshot)
