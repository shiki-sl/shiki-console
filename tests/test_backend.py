"""Security, freshness and independent-delivery tests; temporary loopback only."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from shiki.agent import Agent, agent_initialize, pair_agent, request_json
from shiki.collector import docker_probe, codex_probe, updates_probe
from shiki.common import MAX_BODY, new_token, normalize_snapshot, read_json, write_private
from shiki.receiver import ReceiverServer, Store, dashboard, default_config, initialize, load_receiver, validate_config


def complete_sample(now):
    sample = normalize_snapshot({})
    sample['hardware']['cpu'].update(name='test CPU', usage_percent=10, temperature_c=50, error=None)
    sample['hardware']['memory'].update(used_bytes=25, total_bytes=100, available_bytes=75, error=None)
    sample['hardware']['disks'] = [{'mount': '/', 'used_bytes': 25, 'total_bytes': 100, 'error': None}]
    for item in sample['services'].values():
        item.update(status='ok', checked_at=now, detail='test')
    return sample


class ReceiverFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'receiver'
        initialize(self.directory)
        self.config, self.store = load_receiver(self.directory)
        self.config['listen']['port'] = 0
        self.config['inventory'] = self.config['inventory'][:1]
        self.node = self.config['inventory'][0]['id']
        self.server = ReceiverServer(self.config, self.store)
        self.port = self.server.server_address[1]
        self.config['listen']['allowed_hosts'] = ['127.0.0.1:' + str(self.port)]
        self.url = 'http://127.0.0.1:' + str(self.port)
        self.config['listen']['allowed_origins'] = [self.url]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.viewer = (self.directory / 'viewer.key').read_text().strip()
        self.sender = new_token()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def enroll(self):
        grant = self.store.grant(self.node)
        body = {'device_id': self.node, 'code': grant['code'], 'token': self.sender}
        return body, request_json(self.url + '/api/v1/pair', body)

    def get(self, path='/api/v1/dashboard', token=None, headers=None):
        headers = dict(headers or {})
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with build_opener().open(Request(self.url + path, headers=headers), timeout=2) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            raise

    def report(self, seq=1, when=None, sample=None, token=None, device_id=None):
        when = time.time() if when is None else when
        body = {'schema': 1, 'device_id': device_id or self.node, 'seq': seq,
                'sampled_at': when, 'snapshot': sample or complete_sample(when)}
        return request_json(self.url + '/api/v1/report', body, token or self.sender)

    def test_auth_boundaries_and_private_state(self):
        self.assertTrue(self.get('/api/v1/health')['ok'])
        for token in (None, self.sender):
            with self.assertRaises(HTTPError) as denied:
                self.get(token=token)
            self.assertEqual(denied.exception.code, 401)
        self.enroll()
        second = default_config()['inventory'][1]
        self.config['inventory'].append(second)
        grant = self.store.grant(second['id'])
        self.store.pair(second['id'], grant['code'], new_token())
        with self.assertRaises(HTTPError) as impersonation:
            self.report(device_id=second['id'])
        self.assertEqual(impersonation.exception.code, 401)
        self.assertEqual(self.get(token=self.viewer)['schema'], 1)
        with self.assertRaises(HTTPError) as denied:
            self.report(token=self.viewer)
        self.assertEqual(denied.exception.code, 401)
        with self.assertRaises(HTTPError):
            self.report(device_id='unknown')
        with self.assertRaises(HTTPError) as denied:
            self.get(token=self.viewer, headers={'Origin': 'https://evil.invalid'})
        self.assertEqual(denied.exception.code, 403)
        with self.assertRaises(HTTPError) as denied:
            self.get(token=self.viewer, headers={'Host': 'evil.invalid'})
        self.assertEqual(denied.exception.code, 403)
        with self.assertRaises(HTTPError):
            self.get('/api/v1/dashboard?token=' + self.viewer)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        for name in ('viewer.key', 'receiver.db', 'receiver.json'):
            self.assertEqual((self.directory / name).stat().st_mode & 0o777, 0o600)
        db_bytes = (self.directory / 'receiver.db').read_bytes()
        self.assertNotIn(self.viewer.encode(), db_bytes)
        self.assertNotIn(self.sender.encode(), db_bytes)

    def test_exact_origin_cors_and_configured_receiver_csp(self):
        frontend = 'https://frontend.example.test'
        backup = 'https://backup.example.test:9443'
        self.config['listen']['allowed_origins'].append(frontend)
        self.config['receivers'][1].update(url=backup, state='active')
        headers = {'Origin': frontend, 'Access-Control-Request-Method': 'GET',
                   'Access-Control-Request-Headers': 'authorization'}
        with build_opener().open(Request(self.url + '/api/v1/dashboard', method='OPTIONS', headers=headers)) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers['Access-Control-Allow-Origin'], frontend)
            self.assertEqual(response.headers['Access-Control-Allow-Headers'], 'Authorization')
            self.assertEqual(response.headers['Vary'], 'Origin')
        with self.assertRaises(HTTPError) as auth:
            build_opener().open(Request(self.url + '/api/v1/dashboard', headers={'Origin': frontend}))
        self.assertEqual(auth.exception.code, 401)
        self.assertEqual(auth.exception.headers['Access-Control-Allow-Origin'], frontend)
        auth.exception.close()
        for changed in ({'Origin': 'https://evil.invalid'}, {'Access-Control-Request-Method': 'POST'}, {'Access-Control-Request-Headers': 'x-danger'}):
            with self.assertRaises(HTTPError) as denied:
                build_opener().open(Request(self.url + '/api/v1/dashboard', method='OPTIONS', headers=headers | changed))
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
        with build_opener().open(Request(self.url + '/', headers={'Origin': frontend})) as response:
            csp = response.headers['Content-Security-Policy']
            self.assertIn("connect-src 'self' http://127.0.0.1:8765 " + backup + ';', csp)
            self.assertNotIn('connect-src *', csp)
            self.assertNotIn('https:;', csp)
            self.assertIn(b'<html', response.read().lower())
        with build_opener().open(self.url + '/manifest.webmanifest') as response:
            self.assertEqual(response.headers['Content-Type'], 'application/manifest+json')
            self.assertEqual(json.load(response)['display'], 'standalone')

    def test_pair_replay_revoke_and_expiry(self):
        body, first = self.enroll()
        self.assertTrue(first['accepted'])
        self.assertFalse(request_json(self.url + '/api/v1/pair', body)['accepted'])
        with self.assertRaises(HTTPError):
            request_json(self.url + '/api/v1/pair', dict(body, token=new_token()))
        self.store.revoke(self.node)
        with self.assertRaises(HTTPError):
            request_json(self.url + '/api/v1/pair', body)
        with self.assertRaises(HTTPError):
            self.report()
        grant = self.store.grant(self.node, ttl=-1)
        with self.assertRaises(HTTPError):
            request_json(self.url + '/api/v1/pair', {'device_id': self.node, 'code': grant['code'], 'token': self.sender})

    def test_sequence_retry_timestamps_and_body_limit(self):
        self.enroll()
        now = time.time()
        sample = complete_sample(now)
        self.assertTrue(self.report(1, now, sample)['accepted'])
        received = self.store.rows()[self.node]['received_at']
        self.assertFalse(self.report(1, now, sample)['accepted'])
        self.assertEqual(self.store.rows()[self.node]['received_at'], received)
        changed = copy.deepcopy(sample)
        changed['hardware']['cpu']['usage_percent'] = 99
        for seq, timestamp, item in ((1, now, changed), (0, now, sample), (2, now - 301, sample), (2, now + 121, sample), (2, now - 1, sample)):
            with self.assertRaises(HTTPError) as denied:
                self.report(seq, timestamp, item)
            self.assertEqual(denied.exception.code, 400)
        self.assertTrue(self.report(2, now + .01, sample)['accepted'])
        self.assertEqual(Store(self.directory).rows()[self.node]['seq'], 2)
        with self.assertRaises(HTTPError) as denied:
            build_opener().open(Request(self.url + '/api/v1/report', data=b'x' * (MAX_BODY + 1), headers={'Content-Type': 'application/json'}), timeout=2)
        self.assertEqual(denied.exception.code, 413)
        denied.exception.close()
        too_many = copy.deepcopy(sample)
        too_many['hardware']['gpus'] = [{}] * 17
        with self.assertRaises(HTTPError):
            self.report(3, now + .02, too_many)

    def test_health_freshness_unknown_and_one_alert_per_device(self):
        now = time.time()
        pending = dashboard(self.config, self.store, now)
        self.assertEqual(pending['summary']['state'], 'unknown')
        self.assertEqual(pending['devices'][0]['connection'], 'unregistered')
        self.assertEqual(pending['summary']['attention_count'], 0)
        self.enroll()
        self.report(1, now, complete_sample(now))
        fresh = dashboard(self.config, self.store, now + 1)
        self.assertEqual(fresh['summary']['state'], 'ok')
        self.assertEqual(fresh['policy']['fresh_seconds'], 90)
        self.assertEqual(fresh['receivers'][1]['state'], 'planned')
        stale = dashboard(self.config, self.store, now + 91)
        self.assertEqual(stale['summary']['state'], 'unknown')
        self.assertEqual(stale['devices'][0]['connection'], 'stale')
        offline = dashboard(self.config, self.store, now + 301)
        self.assertEqual(offline['summary']['attention_count'], 1)
        self.assertEqual(offline['devices'][0]['connection'], 'offline')
        hot = complete_sample(now)
        hot['hardware']['cpu']['temperature_c'] = 100
        hot['hardware']['memory']['used_bytes'] = 99
        self.report(2, now + .01, hot)
        self.assertEqual(dashboard(self.config, self.store, now + 1)['summary']['attention_count'], 1)
        unknown = complete_sample(now)
        unknown['hardware']['cpu']['temperature_c'] = None
        self.report(3, now + .02, unknown)
        self.assertEqual(dashboard(self.config, self.store, now + 1)['summary']['state'], 'unknown')
        self.config['inventory'][0]['required_services'] = ['docker']
        old_check = complete_sample(now)
        old_check['services']['docker']['checked_at'] = now - 100
        self.report(4, now + .03, old_check)
        self.assertEqual(dashboard(self.config, self.store, now + 1)['summary']['state'], 'unknown')
        self.config['inventory'][0]['required_services'] = ['updates']
        old_update = complete_sample(now)
        for item in old_update['updates'].values():
            if isinstance(item, dict):
                item.update(state='ok', checked_at=now - 172801)
        self.report(5, now + .04, old_update)
        self.assertEqual(dashboard(self.config, self.store, now + 1)['summary']['state'], 'unknown')

    def test_agent_pair_lost_response_retry_and_receiver_independence(self):
        path = Path(self.temp.name) / 'agent' / 'agent.json'
        agent_initialize(path, self.node)
        grant_file = Path(self.temp.name) / 'grant' / 'code.json'
        write_private(grant_file, self.store.grant(self.node))
        def lost_response(*args, **kwargs):
            request_json(*args, **kwargs)
            raise OSError('simulated lost pairing response')
        with patch('shiki.agent.request_json', side_effect=lost_response):
            with self.assertRaises(OSError):
                pair_agent(path, self.url, 'main', grant_file)
        before = read_json(path)['receivers'][0]['token']
        pair_agent(path, self.url, 'main', grant_file)
        pair_agent(path, self.url, 'main', grant_file)
        self.assertEqual(read_json(path)['receivers'][0]['token'], before)
        config = read_json(path)
        config['receivers'].insert(0, {'id': 'unavailable', 'url': 'http://127.0.0.1:1', 'token': new_token()})
        write_private(path, config)
        agent = Agent(path)
        with patch('shiki.agent.collect_bounded', return_value=complete_sample(time.time())):
            result = agent.run(once=True)
        statuses = {r['receiver_id']: r['status'] for r in result}
        self.assertEqual(statuses['main'], 'ok')
        self.assertEqual(statuses['unavailable'], 'unreachable')
        self.assertEqual(self.store.rows()[self.node]['seq'], 1)
        self.assertEqual(Agent(path).state['seq'], 1)


class ProbeBoundaryTests(unittest.TestCase):
    def test_inactive_docker_never_queries_socket(self):
        with patch('shiki.collector.run') as probe:
            data, status = docker_probe({})
        probe.assert_not_called()
        self.assertIsNone(data['total'])
        self.assertEqual(status['status'], 'unknown')

    def test_native_update_results_preserve_authoritative_time_and_deferred(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            paths = {}
            for name, result in (('system', 'success'), ('cli', 'deferred'), ('gui', 'failed')):
                path = Path(directory) / (name + '.json')
                path.write_text(json.dumps({'result': result, 'last_attempt': now - 3600,
                    'last_success': now - 86400, 'version': '1.2.3', 'error': 'running; defer' if name == 'cli' else None}))
                paths[name] = str(path)
            result, status = updates_probe({'update_status_files': paths})
            self.assertEqual(result['system']['state'], 'ok')
            self.assertEqual(result['cli']['state'], 'deferred')
            self.assertEqual(result['gui']['state'], 'error')
            self.assertEqual(result['cli']['checked_at'], now - 3600)
            self.assertEqual(result['cli']['last_success_at'], now - 86400)
            self.assertEqual(status['checked_at'], now - 3600)
            expired, summary = updates_probe({'update_status_files': paths, 'update_fresh_seconds': 60})
            self.assertTrue(all(expired[k]['state'] == 'unknown' for k in paths))
            self.assertEqual(expired['system']['checked_at'], now - 3600)
            self.assertEqual(summary['status'], 'unknown')
            paths['system'] = str(Path(directory) / 'missing.json')
            unavailable, _ = updates_probe({'update_status_files': paths})
            self.assertEqual(unavailable['system']['state'], 'unknown')
            self.assertIsNone(unavailable['system']['checked_at'])

    def test_docker_stats_merge_and_failure_preserve_inventory(self):
        rows = '\n'.join(json.dumps(row) for row in [
            {'ID': 'a' * 64, 'Names': 'web', 'State': 'running', 'Status': 'Up (healthy)'},
            {'ID': 'b' * 64, 'Names': 'worker', 'State': 'running', 'Status': 'Up'},
            {'ID': 'c' * 64, 'Names': 'old', 'State': 'exited', 'Status': 'Exited'}])
        stats = '\n'.join(json.dumps(row) for row in [
            {'ID': 'a' * 64, 'Name': 'web', 'CPUPerc': '123.45%', 'MemUsage': '1.5MiB / 4GiB'},
            {'ID': 'b' * 64, 'Name': 'worker', 'CPUPerc': '0.0%', 'MemUsage': '250kB / 4GB'}])
        procs = {1: {'exe': 'dockerd', 'name': 'dockerd', 'ppid': 0}}
        with patch('shiki.collector.shutil.which', return_value='/usr/bin/docker'), patch('shiki.collector.run', side_effect=[rows, stats]) as run:
            data, status = docker_probe(procs)
        self.assertEqual(data['running'], 2)
        self.assertEqual(data['containers'][0]['cpu_percent'], 123.45)
        self.assertEqual(data['containers'][0]['memory_bytes'], 1572864)
        self.assertEqual(data['containers'][1]['memory_bytes'], 250000)
        self.assertIsNone(data['containers'][2]['cpu_percent'])
        self.assertIsNone(data['containers'][0]['error'])
        self.assertEqual(run.call_args_list[1].kwargs['timeout'], 3)
        self.assertEqual(run.call_args_list[0].args[0][2], run.call_args_list[1].args[0][2])
        with patch('shiki.collector.shutil.which', return_value='/usr/bin/docker'), patch('shiki.collector.run', side_effect=[rows, PermissionError()]):
            failed, status = docker_probe(procs)
        self.assertEqual(status['status'], 'ok')
        self.assertEqual(failed['total'], 3)
        self.assertIsNone(failed['containers'][0]['cpu_percent'])
        self.assertIn('unavailable', failed['containers'][0]['error'])

    def test_gui_descendant_is_not_standalone_cli(self):
        procs = {1: {'exe': 'ChatGPT', 'name': 'ChatGPT', 'ppid': 0},
                 2: {'exe': 'codex', 'name': 'codex', 'ppid': 1},
                 3: {'exe': 'bash', 'name': 'bash', 'ppid': 0},
                 4: {'exe': 'codex', 'name': 'codex', 'ppid': 3}}
        both, _ = codex_probe(procs)
        self.assertTrue(both['gui_running'])
        self.assertTrue(both['cli_running'])
        del procs[4]
        gui, _ = codex_probe(procs)
        self.assertTrue(gui['gui_running'])
        self.assertFalse(gui['cli_running'])
        self.assertIsNone(gui['realtime_tps'])

    def test_remote_plaintext_and_wildcard_rejected(self):
        config = default_config()
        config['listen']['host'] = '0.0.0.0'
        with self.assertRaises(ValueError):
            validate_config(config)
        config = default_config()
        config['listen']['allowed_hosts'] = ['*']
        with self.assertRaises(ValueError):
            validate_config(config)
        with self.assertRaises(ValueError):
            request_json('http://192.0.2.1/api/v1/report', {})


if __name__ == '__main__':
    unittest.main()
