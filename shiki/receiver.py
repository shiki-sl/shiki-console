"""Local receiver, trust enrollment and freshness-aware dashboard."""
import hmac
from contextlib import contextmanager
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote

from .common import (MAX_BODY, digest, ident, new_token, normalize_snapshot, number,
                     private_dir, private_file, read_json, safe_url, text, write_private)


def default_config():
    inventory = []
    for node_id, name, site, mobile, expected, managed in (
        ('9955hx', 'AMD 9955HX', '主站', False, True, True),
        ('3600', 'AMD 3600', '主站', False, True, True),
        ('cpu', 'cpu · 12400', '异地（计划）', False, True, True),
        ('sd', 'Steam Deck', '移动', True, False, False)):
        inventory.append({'id': node_id, 'name': name, 'site': site,
            'mobility': 'mobile' if mobile else 'fixed', 'expected_online': expected,
            'update_managed': managed, 'required_services': [],
            'required_telemetry': ['cpu', 'memory', 'disks'],
            'thresholds': {'cpu_temperature_c': 90, 'gpu_temperature_c': 90,
                           'memory_percent': 95, 'disk_percent': 90}, 'apps': []})
    return {'receiver': {'id': 'main', 'name': 'Shiki 主接收端', 'site': '主站'},
        'listen': {'host': '127.0.0.1', 'port': 8765,
            'allowed_hosts': ['127.0.0.1:8765', 'localhost:8765'],
            'allowed_origins': ['http://127.0.0.1:8765', 'http://localhost:8765'],
            'tls_cert': None, 'tls_key': None},
        'fresh_seconds': 90, 'offline_seconds': 300, 'update_fresh_seconds': 172800, 'max_clock_skew_seconds': 120,
        'max_sample_age_seconds': 300, 'inventory': inventory,
        'receivers': [{'id': 'main', 'name': '主接收端', 'site': '主站',
            'url': 'http://127.0.0.1:8765', 'role': 'primary', 'state': 'active'},
            {'id': 'backup', 'name': '异地备用接收端', 'site': '异地（计划）',
            'url': None, 'role': 'standby', 'state': 'planned'}]}


def validate_config(config):
    ident(config['receiver']['id'])
    for key in ('name', 'site'):
        text(config['receiver'].get(key))
    listen = config['listen']
    ipaddress.ip_address(listen['host'])
    if isinstance(listen['port'], bool) or not 0 <= listen['port'] <= 65535:
        raise ValueError('invalid listen port')
    if not ipaddress.ip_address(listen['host']).is_loopback and not (listen.get('tls_cert') and listen.get('tls_key')):
        raise ValueError('non-loopback binding requires TLS certificate and key')
    if not listen.get('allowed_hosts') or not listen.get('allowed_origins'):
        raise ValueError('explicit Host and Origin allowlists required')
    for value in listen['allowed_hosts']:
        if '*' in value or '/' in value or len(value) > 256:
            raise ValueError('invalid allowed Host')
    for value in listen['allowed_origins']:
        safe_url(value)
        if '*' in value or urlsplit(value).path not in ('', '/') or urlsplit(value).query:
            raise ValueError('invalid allowed Origin')
    if not 5 <= config.get('fresh_seconds', 90) < config.get('offline_seconds', 300) <= 86400:
        raise ValueError('invalid freshness thresholds')
    if not 60 <= config.get('update_fresh_seconds', 172800) <= 2592000:
        raise ValueError('invalid update freshness threshold')
    for key in ('max_clock_skew_seconds', 'max_sample_age_seconds'):
        if not 1 <= config.get(key, 120) <= 86400:
            raise ValueError('invalid timestamp tolerance')
    nodes = config.get('inventory', [])
    if len(nodes) > 64:
        raise ValueError('too many inventory entries')
    seen = set()
    for node in nodes:
        node_id = ident(node['id'])
        if node_id in seen:
            raise ValueError('duplicate device')
        seen.add(node_id)
        for key in ('name', 'site'):
            text(node.get(key))
        if node.get('mobility', 'fixed') not in ('fixed', 'mobile'):
            raise ValueError('invalid mobility')
        for key in ('expected_online', 'update_managed'):
            if not isinstance(node.get(key), bool):
                raise ValueError('inventory flags must be booleans')
        if any(v not in ('cpu', 'memory', 'disks', 'gpu') for v in node.get('required_telemetry', [])):
            raise ValueError('invalid required telemetry')
        if any(v not in ('codex', 'docker', 'tailscale', 'sunshine', 'loki', 'grafana', 'updates') for v in node.get('required_services', [])):
            raise ValueError('invalid required service')
        for value in node.get('thresholds', {}).values():
            number(value, 0, 250)
        if len(node.get('apps', [])) > 32:
            raise ValueError('too many app links')
        for app in node.get('apps', []):
            ident(app['id'])
            text(app['name'])
            text(app.get('description'))
            safe_url(app['url'])
            if app.get('access') not in ('lan', 'tailnet', 'public') or not isinstance(app.get('enabled', True), bool):
                raise ValueError('invalid app link metadata')
    if len(config.get('receivers', [])) > 16:
        raise ValueError('too many receivers')
    for receiver in config.get('receivers', []):
        ident(receiver['id'])
        if receiver.get('url'):
            safe_url(receiver['url'], transport=True)
        if receiver.get('role') not in ('primary', 'standby') or receiver.get('state') not in ('active', 'planned'):
            raise ValueError('invalid receiver role/state')
    return config


class Store:
    def __init__(self, directory):
        self.directory = private_dir(directory)
        self.db_path = private_file(self.directory / 'receiver.db')
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0, report_hash TEXT, sampled_at REAL,
                    received_at REAL, snapshot TEXT);
                CREATE TABLE IF NOT EXISTS grants (device_id TEXT PRIMARY KEY, code_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL, consumed_token_hash TEXT);
            ''')
        private_file(self.db_path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def set_viewer(self, token):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('viewer_hash', digest(token)))

    def viewer_ok(self, token):
        with self.connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', ('viewer_hash',)).fetchone()
        return bool(row and hmac.compare_digest(row[0], digest(token)))

    def grant(self, device_id, ttl=300):
        code = new_token()
        expires = time.time() + ttl
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO grants VALUES (?,?,?,NULL)', (device_id, digest(code), expires))
        return {'device_id': device_id, 'code': code, 'expires_at': expires}

    def pair(self, device_id, code, token, now=None):
        now = time.time() if now is None else now
        if not isinstance(code, str) or not 32 <= len(code) <= 128 or not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise PermissionError('invalid pairing credentials')
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT code_hash,expires_at,consumed_token_hash FROM grants WHERE device_id=?', (device_id,)).fetchone()
            if not row or row[1] < now or not hmac.compare_digest(row[0], digest(code)):
                raise PermissionError('invalid or expired pairing grant')
            hashed = digest(token)
            if row[2]:
                if not hmac.compare_digest(row[2], hashed):
                    raise PermissionError('pairing grant already consumed')
                current = db.execute('SELECT token_hash FROM devices WHERE id=?', (device_id,)).fetchone()
                if not current or not hmac.compare_digest(current[0], hashed):
                    raise PermissionError('pairing enrollment revoked')
                return False
            # Re-enrollment creates a new epoch: sender can resume from any positive sequence.
            db.execute('INSERT OR REPLACE INTO devices (id,token_hash,seq) VALUES (?,?,0)', (device_id, hashed))
            db.execute('UPDATE grants SET consumed_token_hash=? WHERE device_id=?', (hashed, device_id))
        return True

    def revoke(self, device_id):
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM devices WHERE id=?', (device_id,))
            db.execute('DELETE FROM grants WHERE device_id=?', (device_id,))

    def report(self, report, token, config, now=None):
        now = time.time() if now is None else now
        device_id = ident(report.get('device_id'))
        if report.get('schema') != 1 or isinstance(report.get('schema'), bool):
            raise ValueError('unsupported schema')
        seq = report.get('seq')
        if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= 9223372036854775807:
            raise ValueError('invalid sequence')
        sampled_at = number(report.get('sampled_at'))
        if sampled_at is None or sampled_at > now + config.get('max_clock_skew_seconds', 120) or sampled_at < now - config.get('max_sample_age_seconds', 300):
            raise ValueError('sample timestamp outside acceptance window')
        normalized = normalize_snapshot(report.get('snapshot'))
        payload = json.dumps(normalized, sort_keys=True, separators=(',', ':'), allow_nan=False)
        hashed = digest(json.dumps([sampled_at, payload], separators=(',', ':')))
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT token_hash,seq,report_hash,sampled_at FROM devices WHERE id=?', (device_id,)).fetchone()
            if not row or not hmac.compare_digest(row[0], digest(token)):
                raise PermissionError('sender authentication failed')
            if seq == row[1] and row[2] == hashed:
                return False
            if seq <= row[1] or (row[3] is not None and sampled_at < row[3]):
                raise ValueError('older or conflicting report')
            db.execute('UPDATE devices SET seq=?,report_hash=?,sampled_at=?,received_at=?,snapshot=? WHERE id=?',
                (seq, hashed, sampled_at, now, payload, device_id))
        return True

    def rows(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return {r['id']: dict(r) for r in db.execute('SELECT id,seq,sampled_at,received_at,snapshot FROM devices')}


def initialize(directory):
    directory = private_dir(directory)
    config_path, key_path = directory / 'receiver.json', directory / 'viewer.key'
    if config_path.exists() or key_path.exists() or (directory / 'receiver.db').exists():
        raise ValueError('refusing to overwrite existing receiver state')
    config = default_config()
    write_private(config_path, config)
    token = new_token()
    write_private(key_path, token + '\n')
    Store(directory).set_viewer(token)
    return config


def issue(component, severity, message):
    return {'component': component, 'severity': severity, 'message': message}


def device_view(node, row, config, now):
    sampled = row['sampled_at'] if row else None
    received = row['received_at'] if row else None
    age = max(0, now - sampled) if sampled is not None else None
    snapshot = json.loads(row['snapshot']) if row and row['snapshot'] else None
    connection = ('unregistered' if row is None else 'offline' if age is None or age > config.get('offline_seconds', 300)
                  else 'stale' if age > config.get('fresh_seconds', 90) else 'online')
    issues = []
    expected = node.get('expected_online', True)
    if connection == 'unregistered':
        issues.append(issue('connection', 'unknown', '尚未配对注册'))
    elif connection != 'online':
        severity = 'error' if expected and connection == 'offline' else 'unknown'
        issues.append(issue('connection', severity, '样本已过期' if age is not None else '已注册，等待首个样本'))
    if snapshot and connection == 'online':
        hardware = snapshot['hardware']
        required = node.get('required_telemetry', ['cpu', 'memory', 'disks'])
        for kind in required:
            item = hardware.get(kind if kind != 'gpu' else 'gpus')
            complete = False
            if kind == 'cpu':
                complete = item and item['usage_percent'] is not None and item['temperature_c'] is not None and not item['error']
            elif kind == 'memory':
                complete = item and item['used_bytes'] is not None and bool(item['total_bytes']) and not item['error']
            elif kind == 'disks':
                complete = item and all(v['used_bytes'] is not None and v['total_bytes'] and not v['error'] for v in item)
            elif kind == 'gpu':
                complete = item and all(v['usage_percent'] is not None and v['temperature_c'] is not None and not v['error'] for v in item)
            if not complete:
                issues.append(issue(kind, 'unknown', '必要指标不可用或未完整采集'))
        for name in node.get('required_services', []):
            service = snapshot['services'][name]
            checked = service['checked_at']
            max_age = config.get('update_fresh_seconds', 172800) if name == 'updates' else config.get('fresh_seconds', 90)
            fresh = checked is not None and 0 <= now - checked <= max_age
            if name == 'updates':
                fresh = fresh and all(item['checked_at'] is not None and 0 <= now - item['checked_at'] <= max_age
                    for item in snapshot['updates'].values() if isinstance(item, dict))
            if not fresh or service['status'] == 'unknown':
                issues.append(issue(name, 'unknown', '服务状态未知或检查已过期'))
            elif service['status'] != 'ok':
                issues.append(issue(name, 'error', service['detail'] or '必要服务不可用'))
        thresholds = node.get('thresholds', {})
        temp = hardware['cpu']['temperature_c']
        if temp is not None and temp >= thresholds.get('cpu_temperature_c', 90):
            issues.append(issue('cpu', 'warning', 'CPU 温度达到阈值'))
        if any(v['temperature_c'] is not None and v['temperature_c'] >= thresholds.get('gpu_temperature_c', 90) for v in hardware['gpus']):
            issues.append(issue('gpu', 'warning', 'GPU 温度达到阈值'))
        memory = hardware['memory']
        if memory['total_bytes'] and memory['used_bytes'] is not None and memory['used_bytes'] * 100 / memory['total_bytes'] >= thresholds.get('memory_percent', 95):
            issues.append(issue('memory', 'warning', '内存使用达到阈值'))
        if any(v['total_bytes'] and v['used_bytes'] is not None and v['used_bytes'] * 100 / v['total_bytes'] >= thresholds.get('disk_percent', 90) for v in hardware['disks']):
            issues.append(issue('disks', 'warning', '磁盘使用达到阈值'))
        if snapshot['docker']['status'] == 'ok' and any(v['health'] == 'unhealthy' for v in snapshot['docker']['containers']):
            issues.append(issue('docker', 'warning', '容器健康检查失败'))
        # Update execution is never inferred from a process or a systemd exit code.
        if node.get('update_managed'):
            for kind, update in snapshot['updates'].items():
                if (isinstance(update, dict) and update['state'] == 'error'
                        and update['checked_at'] is not None
                        and 0 <= now - update['checked_at'] <= config.get('update_fresh_seconds', 172800)):
                    issues.append(issue('updates', 'warning', kind + ': ' + (update['detail'] or '更新记录报告失败')))
    health = 'attention' if any(v['severity'] in ('error', 'warning') for v in issues) else 'unknown' if issues else 'ok'
    return {key: node.get(key) for key in ('id', 'name', 'site', 'mobility', 'expected_online', 'update_managed', 'required_services')} | {
        'registered': row is not None, 'connection': connection, 'sampled_at': sampled,
        'received_at': received, 'age_seconds': age, 'health': health, 'issues': issues,
        'snapshot': snapshot, 'apps': node.get('apps', [])}


def dashboard(config, store, now=None):
    now = time.time() if now is None else now
    rows = store.rows()
    devices = [device_view(n, rows.get(n['id']), config, now) for n in config['inventory']]
    # Count confirmed device alerts once; optional/offline mobile nodes are not outages.
    attention = sum(d['health'] == 'attention' for d in devices)
    expected = [d for d in devices if d['expected_online']]
    unknown = sum(d['health'] == 'unknown' for d in expected)
    state = 'attention' if attention else 'unknown' if unknown or not expected else 'ok'
    headline = 'ALL SYSTEMS OPERATIONAL' if state == 'ok' else '需要关注' if state == 'attention' else '等待完整且新鲜的遥测'
    receiver = {**config['receiver'], 'server_time': now}
    return {'schema': 1, 'receiver': receiver, 'summary': {'state': state, 'attention_count': attention,
        'unknown_count': unknown, 'headline': headline}, 'policy': {'fresh_seconds': config.get('fresh_seconds', 90), 'offline_seconds': config.get('offline_seconds', 300), 'update_fresh_seconds': config.get('update_fresh_seconds', 172800)}, 'receivers': config.get('receivers', []), 'devices': devices}


class ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config, store, web_dir=None):
        self.config, self.store = validate_config(config), store
        self.web_dir = Path(web_dir or Path(__file__).parent / 'web').resolve()
        super().__init__((config['listen']['host'], config['listen']['port']), Handler)
        if config['listen'].get('tls_cert') and config['listen'].get('tls_key'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(config['listen']['tls_cert'], config['listen']['tls_key'])
            self.socket = context.wrap_socket(self.socket, server_side=True)


class Handler(BaseHTTPRequestHandler):
    server_version = 'Shiki/1'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        # Paths, credentials and bodies never enter request logs.
        pass

    def send_payload(self, status, payload, content_type='application/json; charset=utf-8', extra_headers=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        origin = self.headers.get('Origin')
        if self.path == '/api/v1/dashboard' and origin in self.server.config['listen']['allowed_origins']:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        receiver_origins = sorted({urlsplit(r['url']).scheme + '://' + urlsplit(r['url']).netloc for r in self.server.config.get('receivers', []) if r.get('url')})
        connect_sources = "'self'" + (' ' + ' '.join(receiver_origins) if receiver_origins else '')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src " + connect_sources + "; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def boundary(self):
        listen = self.server.config['listen']
        hosts = self.headers.get_all('Host', [])
        origins = self.headers.get_all('Origin', [])
        if len(hosts) != 1 or hosts[0] not in listen['allowed_hosts']:
            self.send_payload(403, {'error': 'Host not allowed'})
            return False
        if len(origins) > 1 or (origins and origins[0] not in listen['allowed_origins']):
            self.send_payload(403, {'error': 'Origin not allowed'})
            return False
        if urlsplit(self.path).query or not self.path.startswith('/') or self.path.startswith('//'):
            self.send_payload(400, {'error': 'query parameters and absolute URLs are not supported'})
            return False
        return True

    def bearer(self):
        values = self.headers.get_all('Authorization', [])
        if len(values) != 1 or not values[0].startswith('Bearer '):
            return ''
        token = values[0][7:]
        return token if 32 <= len(token) <= 128 else ''

    def do_OPTIONS(self):
        if not self.boundary():
            return
        requested = {item.strip().lower() for item in self.headers.get('Access-Control-Request-Headers', '').split(',') if item.strip()}
        if (self.path != '/api/v1/dashboard' or not self.headers.get('Origin')
                or self.headers.get('Access-Control-Request-Method') != 'GET'
                or not requested <= {'authorization'}):
            self.send_payload(403, {'error': 'preflight not allowed'})
            return
        self.send_payload(204, b'', extra_headers={'Access-Control-Allow-Methods': 'GET',
            'Access-Control-Allow-Headers': 'Authorization', 'Access-Control-Max-Age': '300'})

    def do_GET(self):
        if not self.boundary():
            return
        if self.path == '/api/v1/health':
            self.send_payload(200, {'ok': True, 'receiver_id': self.server.config['receiver']['id'], 'server_time': time.time()})
        elif self.path == '/api/v1/dashboard':
            if not self.server.store.viewer_ok(self.bearer()):
                self.send_payload(401, {'error': 'viewer authentication required'})
                return
            self.send_payload(200, dashboard(self.server.config, self.server.store))
        elif self.path.startswith('/api/'):
            self.send_payload(404, {'error': 'not found'})
        else:
            relative = unquote(self.path).lstrip('/') or 'index.html'
            target = (self.server.web_dir / relative).resolve()
            if not target.is_relative_to(self.server.web_dir) or not target.is_file() or target.suffix not in ('.html', '.js', '.css', '.json', '.svg', '.png', '.ico', '.webmanifest'):
                self.send_payload(404, {'error': 'not found'})
                return
            self.send_payload(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or 'application/octet-stream')

    def do_POST(self):
        if not self.boundary():
            return
        if self.path not in ('/api/v1/pair', '/api/v1/report'):
            self.send_payload(404, {'error': 'not found'})
            return
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                raise ValueError('explicit Content-Length required')
            length = int(lengths[0])
            if not 1 <= length <= MAX_BODY:
                self.send_payload(413, {'error': 'body too large or empty'})
                return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('application/json required')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete request')
            report = json.loads(raw)
            if not isinstance(report, dict):
                raise ValueError('object required')
            device_id = ident(report.get('device_id'))
            if device_id not in {n['id'] for n in self.server.config['inventory']}:
                raise PermissionError('device not in trusted inventory')
            if self.path.endswith('/pair'):
                changed = self.server.store.pair(device_id, report.get('code'), report.get('token'))
            else:
                changed = self.server.store.report(report, self.bearer(), self.server.config)
            self.send_payload(200, {'ok': True, 'accepted': changed, 'receiver_id': self.server.config['receiver']['id']})
        except PermissionError:
            self.send_payload(401, {'error': 'authentication or enrollment rejected'})
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            self.send_payload(400, {'error': 'invalid report or pairing request'})
        except sqlite3.Error:
            self.send_payload(503, {'error': 'receiver storage unavailable'})


def load_receiver(directory):
    directory = private_dir(directory)
    config_path = private_file(directory / 'receiver.json')
    return validate_config(read_json(config_path)), Store(directory)
