"""Independent latest-only delivery to explicitly paired receivers."""
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import queue
import signal
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler
import ssl

from .collector import DEFAULT_UPDATE_STATUS_FILES, NoRedirect, collect
from .common import (MAX_BODY, digest, ident, new_token, normalize_snapshot, private_dir,
                     private_file, read_json, safe_url, write_private)


def agent_initialize(path, device_id):
    path = Path(path).expanduser().absolute()
    if path.exists():
        raise ValueError('refusing to overwrite existing agent configuration')
    config = {'device_id': ident(device_id), 'interval_seconds': 15,
        'collection_timeout_seconds': 8, 'disks': ['/'], 'receivers': [],
        'known_health_urls': {}, 'update_metadata_file': None, 'update_status_files': dict(DEFAULT_UPDATE_STATUS_FILES),
        'update_fresh_seconds': 172800, 'codex_metadata': False}
    write_private(path, config)
    return config


def validate_agent(config):
    ident(config['device_id'])
    if not 5 <= config.get('interval_seconds', 15) <= 3600:
        raise ValueError('agent interval must be 5..3600 seconds')
    if not 2 <= config.get('collection_timeout_seconds', 8) <= 30:
        raise ValueError('collection timeout must be 2..30 seconds')
    if len(config.get('disks', ['/'])) > 32 or any(not isinstance(p, str) or not p.startswith('/') or len(p) > 256 for p in config.get('disks', ['/'])):
        raise ValueError('invalid configured disk mounts')
    if not 60 <= config.get('update_fresh_seconds', 172800) <= 2592000:
        raise ValueError('update freshness must be 60..2592000 seconds')
    for name, path in config.get('update_status_files', {}).items():
        if name not in ('system', 'cli', 'gui') or (path is not None and (not isinstance(path, str) or len(path) > 1024)):
            raise ValueError('invalid native updater path')
    seen = set()
    if len(config.get('receivers', [])) > 16:
        raise ValueError('too many receivers')
    for target in config.get('receivers', []):
        ident(target['id'])
        if target['id'] in seen:
            raise ValueError('duplicate receiver identifier')
        seen.add(target['id'])
        safe_url(target['url'], transport=True)
        token = target.get('token', '')
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError('invalid sender token')
    for name, url in config.get('known_health_urls', {}).items():
        if name not in ('loki', 'grafana'):
            raise ValueError('only configured Grafana/Loki health endpoints supported')
        safe_url(url)
    return config


def request_json(url, body, token=None, timeout=3, ca_file=None):
    safe_url(url.rsplit('/api/v1/', 1)[0], transport=True)
    encoded = json.dumps(body, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_BODY:
        raise ValueError('report exceeds protocol limit')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request(url, data=encoded, headers=headers, method='POST')
    context = ssl.create_default_context(cafile=ca_file)
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError('receiver response too large')
            result = json.loads(data)
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('receiver did not acknowledge request')
            return result
    except HTTPError as exc:
        exc.close()
        raise


class ConfigLock:
    def __init__(self, path):
        self.path = Path(str(path) + '.lock')
        self.file = None

    def __enter__(self):
        private_dir(self.path.parent)
        private_file(self.path)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        self.file = os.fdopen(fd, 'a+')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise ValueError('agent configuration already in use') from None
        return self

    def __exit__(self, *_args):
        self.file.close()


def pair_agent(path, url, receiver_id, code_file):
    path = Path(path).expanduser().absolute()
    safe_url(url, transport=True)
    ident(receiver_id)
    with ConfigLock(path):
        private_file(path)
        config = read_json(path)
        grant = read_json(private_file(Path(code_file)))
        if grant.get('device_id') != config['device_id']:
            raise ValueError('pairing grant is for another device')
        if grant.get('expires_at', 0) < time.time():
            raise ValueError('pairing grant expired')
        code = grant['code']
        targets = config.setdefault('receivers', [])
        target = next((t for t in targets if t['id'] == receiver_id), None)
        if target is None:
            target = {'id': receiver_id}
            targets.append(target)
        marker = digest(code)
        if target.get('pending_grant_hash') != marker:
            target['token'] = new_token()
            target['pending_grant_hash'] = marker
        target.update(url=url.rstrip('/'), enabled=True)
        validate_agent(config)
        # Persist BEFORE network request; a lost response can retry identical code+token.
        write_private(path, config)
        response = request_json(target['url'] + '/api/v1/pair', {'device_id': config['device_id'],
            'code': code, 'token': target['token']}, ca_file=target.get('ca_file'))
        if response.get('receiver_id') != receiver_id:
            raise ValueError('receiver identity mismatch; enrollment outcome requires local review')
        # Keep the hash to make repeating the same local pairing command idempotent.
        target['paired_at'] = time.time()
        write_private(path, config)
        return response


def _collect_process(config, pipe):
    try:
        pipe.send(collect(config))
    except Exception:
        pipe.send(None)
    finally:
        pipe.close()


def collect_bounded(config):
    context = multiprocessing.get_context('spawn')
    reader, writer = context.Pipe(duplex=False)
    probe_config = {key: config[key] for key in ('collection_timeout_seconds', 'disks', 'known_health_urls', 'update_metadata_file', 'update_status_files', 'update_fresh_seconds', 'codex_metadata') if key in config}
    process = context.Process(target=_collect_process, args=(probe_config, writer), daemon=True)
    process.start()
    writer.close()
    result = None
    try:
        if reader.poll(config.get('collection_timeout_seconds', 8) + .5):
            try:
                result = reader.recv()
            except EOFError:
                pass
    finally:
        reader.close()
        process.join(.1)
        if process.is_alive():
            process.terminate()
            process.join(.3)
        if process.is_alive():
            process.kill()
            process.join()
    if result is None:
        result = normalize_snapshot({})
        for service in result['services'].values():
            service.update(status='unknown', checked_at=time.time(), detail='Collection timeout or unsupported platform')
        result['hardware']['cpu']['error'] = 'Collection deadline exceeded'
        result['hardware']['memory']['error'] = 'Collection deadline exceeded'
    return result


class Agent:
    def __init__(self, path, config=None):
        self.path = Path(path).expanduser().absolute()
        private_file(self.path)
        self.config = validate_agent(config or read_json(self.path))
        self.state_path = self.path.with_name(self.path.stem + '.state.json')
        private_file(self.state_path)
        self.state = read_json(self.state_path) if self.state_path.exists() else {'seq': 0, 'delivery': {}}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.targets = [r for r in self.config['receivers'] if r.get('enabled', True)]
        self.queues = {r['id']: queue.Queue(maxsize=1) for r in self.targets}
        self.workers = []
        self.interval = self.config.get('interval_seconds', 15)

    def save(self):
        write_private(self.state_path, self.state)

    def delivery(self, target, status, error=None):
        with self.lock:
            old = self.state['delivery'].get(target['id'], {})
            self.state['delivery'][target['id']] = {'receiver_id': target['id'], 'status': status,
                'last_success_at': time.time() if status == 'ok' else old.get('last_success_at'), 'error': error}
            self.save()

    def deliver(self, target, report):
        try:
            response = request_json(target['url'] + '/api/v1/report', report, target['token'],
                                    timeout=3, ca_file=target.get('ca_file'))
            if response.get('receiver_id') != target['id']:
                raise ValueError('receiver identity mismatch')
            self.delivery(target, 'ok')
            return True
        except HTTPError as exc:
            status = 'unauthorized' if exc.code in (401, 403) else 'error'
            self.delivery(target, status, 'Receiver rejected report (HTTP ' + str(exc.code) + ')')
        except (OSError, ValueError, URLError):
            self.delivery(target, 'unreachable', 'Receiver unreachable, invalid acknowledgment or TLS validation failed')
        return False

    def sample(self):
        sampled_at = time.time()
        snapshot = collect_bounded(self.config)
        with self.lock:
            self.state['seq'] += 1
            seq = self.state['seq']
            self.save()  # Sequence cannot rewind after process restart or lost response.
            snapshot['delivery'] = list(self.state['delivery'].values())
        return {'schema': 1, 'device_id': self.config['device_id'], 'seq': seq,
                'sampled_at': sampled_at, 'snapshot': snapshot}

    def worker(self, target):
        inbox = self.queues[target['id']]
        report, failures = None, 0
        next_attempt = 0
        while not self.stop.is_set():
            try:
                latest = inbox.get(timeout=min(.5, max(.01, next_attempt - time.monotonic())) if report is not None else .5)
                report = latest
            except queue.Empty:
                pass
            if report is None or time.monotonic() < next_attempt:
                continue
            if self.deliver(target, report):
                report, failures = None, 0
                next_attempt = 0
            else:
                failures += 1
                # No historical queue. Next interval replaces failed payload with current sample.
                next_attempt = time.monotonic() + min(300, max(self.interval, 2 ** min(failures, 8)))

    def run(self, once=False):
        if not self.targets:
            raise ValueError('no paired receiver configured')
        if once:
            report = self.sample()
            threads = [threading.Thread(target=self.deliver, args=(target, report)) for target in self.targets]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            return list(self.state['delivery'].values())
        for target in self.targets:
            thread = threading.Thread(target=self.worker, args=(target,), daemon=True, name='deliver-' + target['id'])
            thread.start()
            self.workers.append(thread)
        try:
            while not self.stop.is_set():
                start = time.monotonic()
                report = self.sample()
                for inbox in self.queues.values():
                    try:
                        inbox.get_nowait()
                    except queue.Empty:
                        pass
                    inbox.put_nowait(report)
                self.stop.wait(max(0, self.interval - (time.monotonic() - start)))
        finally:
            self.stop.set()
            for thread in self.workers:
                thread.join(4)
        return list(self.state['delivery'].values())
