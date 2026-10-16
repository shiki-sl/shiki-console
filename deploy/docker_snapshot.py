#!/usr/bin/python3
"""Standalone root-owned Docker telemetry writer; no agent/repository imports.

Install root:root 0755 under /usr/local/libexec and execute with /usr/bin/python3 -I.
Fixed output: /run/shiki-docker/status.json, root:shiki 0640 in root:shiki 0750.
The only subprocesses are fixed systemctl show and fixed Docker ps/stats queries.
"""
import grp
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time

DIRECTORY = Path('/run/shiki-docker')
OUTPUT = DIRECTORY / 'status.json'
DOCKER = '/usr/bin/docker'
SYSTEMCTL = '/usr/bin/systemctl'
LIMIT = 131072
PS_FORMAT = '{"ID":{{json .ID}},"Names":{{json .Names}},"State":{{json .State}},"Status":{{json .Status}}}'
STATS_FORMAT = '{"ID":{{json .ID}},"Name":{{json .Name}},"CPUPerc":{{json .CPUPerc}},"MemUsage":{{json .MemUsage}}}'


def trusted_root_path(path, directory=False):
    """Reject symlinks and every writable/non-root-owned ancestor."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute root-owned path required')
    for part in reversed((path,) + tuple(path.parents)):
        info = part.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError('unsafe root trust chain')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('invalid parent directory')
    info = path.lstat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError('directory required')
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError('regular file required')
    return info


def command(argv, config_dir, timeout, limit=65536):
    """No inherited Docker context/config/API version, shell or arbitrary argv."""
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(config_dir),
        'DOCKER_CONFIG': str(config_dir), 'LC_ALL': 'C'}
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=str(config_dir), env=environment,
        close_fds=True, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    data = bytearray()
    try:
        while time.monotonic() < deadline:
            if selector.select(min(.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(process.stdout.fileno(), min(8192, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('query output exceeds limit')
        else:
            raise TimeoutError('query timeout')
        if process.wait(timeout=max(.01, deadline - time.monotonic())):
            raise ValueError('query failed')
        return data.decode('utf-8')
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()


def bounded_text(value, length=256):
    if not isinstance(value, str) or len(value) > length or any(ord(c) < 32 for c in value):
        raise ValueError('invalid Docker field')
    return value


def rows(raw):
    lines = raw.splitlines()
    if len(lines) > 128:
        raise ValueError('container limit exceeded')
    result = [json.loads(line) for line in lines]
    if not all(isinstance(row, dict) for row in result):
        raise ValueError('invalid Docker records')
    return result


def memory_bytes(value):
    found = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]i?B|B)\s*', value, re.I)
    if not found:
        return None
    unit = found[2].lower()
    power = 'bkmgtpe'.index(unit[0])
    size = float(found[1]) * (1024 if 'i' in unit else 1000) ** power
    return round(size) if math.isfinite(size) and 0 <= size <= 1e18 else None


def unavailable(status, message, sampled_at):
    return {'schema': 1, 'sampled_at': sampled_at,
        'docker': {'status': status, 'total': None, 'running': None, 'exited': None,
                   'containers': [], 'error': message},
        'service': {'status': status, 'checked_at': sampled_at, 'detail': message}}


def collect(config_dir):
    sampled_at = time.time()
    try:
        # show does not start the service. Never query Docker unless native service
        # is already active with its confirmed dockerd PID.
        raw = command([SYSTEMCTL, 'show', 'docker.service', '--property=ActiveState,SubState,MainPID'],
                      config_dir, timeout=1, limit=2048)
        state = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        pid = int(state.get('MainPID', '0'))
        if state.get('ActiveState') in ('inactive', 'failed') and pid == 0:
            return unavailable('error', 'Docker is installed but the native daemon is not running; no socket query performed', sampled_at)
        if state.get('ActiveState') != 'active' or pid <= 0:
            return unavailable('unknown', 'Native Docker service is not confirmed running; no socket query performed', sampled_at)
        # comm is readable without CAP_SYS_PTRACE; systemd supplies the trusted
        # MainPID. Do not dereference /proc/PID/exe under the empty capability set.
        with open('/proc/' + str(pid) + '/comm', 'r', encoding='ascii') as stream:
            daemon_name = stream.read(64).strip()
        if daemon_name != 'dockerd':
            return unavailable('unknown', 'Native Docker service PID is not dockerd; no socket query performed', sampled_at)
        trusted_root_path('/usr/bin/dockerd')
        socket = Path('/run/docker.sock')
        socket_info = socket.lstat()
        if socket_info.st_uid != 0 or not stat.S_ISSOCK(socket_info.st_mode):
            return unavailable('unknown', 'Expected native Docker socket is unavailable; no socket query performed', sampled_at)
        prefix = [DOCKER, '--host', 'unix:///run/docker.sock']
        containers = []
        for item in rows(command(prefix + ['ps', '--all', '--no-trunc', '--format', PS_FORMAT], config_dir, timeout=2)):
            status = bounded_text(item.get('Status', ''))
            containers.append({'id': bounded_text(item.get('ID', ''), 64),
                'name': bounded_text(item.get('Names', '')), 'state': bounded_text(item.get('State', 'unknown'), 64),
                'health': 'unhealthy' if '(unhealthy)' in status else 'healthy' if '(healthy)' in status else None,
                'cpu_percent': None, 'memory_bytes': None,
                'error': 'Container resource counters unavailable for this state/sample'})
        running = [item for item in containers if item['state'] == 'running']
        if running:
            try:
                stats = rows(command(prefix + ['stats', '--no-stream', '--no-trunc', '--format', STATS_FORMAT], config_dir, timeout=3))
                for item in running:
                    source = next((row for row in stats if row.get('ID') == item['id'] or row.get('Name') == item['name']), None)
                    if source is None:
                        continue
                    try:
                        cpu = float(str(source.get('CPUPerc', '')).strip().rstrip('%'))
                        item['cpu_percent'] = cpu if math.isfinite(cpu) and 0 <= cpu <= 100000 else None
                    except ValueError:
                        pass
                    item['memory_bytes'] = memory_bytes(str(source.get('MemUsage', '')).split('/')[0])
                    item['error'] = None if item['cpu_percent'] is not None and item['memory_bytes'] is not None else 'Some container resource counters unavailable'
            except (OSError, ValueError, TypeError, TimeoutError, subprocess.TimeoutExpired):
                for item in running:
                    item['error'] = 'Container stats unavailable; inventory remains valid'
        return {'schema': 1, 'sampled_at': sampled_at,
            'docker': {'status': 'ok', 'total': len(containers), 'running': len(running),
                'exited': sum(item['state'] == 'exited' for item in containers), 'containers': containers, 'error': None},
            'service': {'status': 'ok', 'checked_at': sampled_at,
                'detail': 'Native Docker read-only root snapshot; container resource availability is independent'}}
    except (OSError, ValueError, TypeError, TimeoutError, subprocess.TimeoutExpired):
        return unavailable('unknown', 'Native Docker read-only snapshot query failed or timed out', sampled_at)


def write_snapshot(snapshot, group_id):
    info = trusted_root_path(DIRECTORY, directory=True)
    if stat.S_IMODE(info.st_mode) != 0o750 or info.st_gid != group_id:
        raise ValueError('runtime directory must be root:shiki 0750')
    if OUTPUT.exists() or OUTPUT.is_symlink():
        trusted_root_path(OUTPUT)
    data = json.dumps(snapshot, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(data) > LIMIT:
        raise ValueError('snapshot exceeds limit')
    descriptor, temporary = tempfile.mkstemp(prefix='.status-', dir=DIRECTORY)
    try:
        os.fchown(descriptor, 0, group_id)
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, OUTPUT)
        directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if os.geteuid() != 0:
        raise SystemExit('This fixed snapshot helper requires the configured root service')
    os.umask(0o027)
    # An isolated interpreter plus root-owned source and every ancestor prevents
    # user-agent edits from becoming executable root code.
    trusted_root_path(Path(__file__).absolute())
    trusted_root_path(DOCKER)
    trusted_root_path(SYSTEMCTL)
    info = trusted_root_path(DIRECTORY, directory=True)
    group_id = grp.getgrnam('shiki').gr_gid
    if stat.S_IMODE(info.st_mode) != 0o750 or info.st_gid != group_id:
        raise SystemExit('Runtime directory must be root:shiki 0750')
    with tempfile.TemporaryDirectory(prefix='.docker-config-', dir=DIRECTORY) as config_dir:
        os.chmod(config_dir, 0o700)
        snapshot = collect(config_dir)
    write_snapshot(snapshot, group_id)


if __name__ == '__main__':
    main()
