"""Privilege boundary tests use mock root metadata, never root or real Docker."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

from shiki.agent import validate_agent
from shiki.collector import docker_probe, docker_snapshot_probe

spec = importlib.util.spec_from_file_location('snapshot_helper', Path(__file__).parent.parent / 'deploy/docker_snapshot.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class DockerSnapshotTests(unittest.TestCase):
    def fixture(self, now):
        return {'schema': 1, 'sampled_at': now,
            'docker': {'status': 'ok', 'total': 1, 'running': 1, 'exited': 0, 'error': None,
                'containers': [{'id': 'a' * 64, 'name': 'web', 'state': 'running',
                    'health': 'healthy', 'cpu_percent': 3.5, 'memory_bytes': 1234, 'error': None}]},
            'service': {'status': 'ok', 'checked_at': now, 'detail': 'Native read-only snapshot'}}

    def test_reader_trust_freshness_and_inactive_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            now = time.time()
            source = self.fixture(now)
            path.write_text(json.dumps(source))
            path.chmod(0o640)
            # Real user-owned input must fail closed even when its JSON is valid.
            self.assertEqual(docker_snapshot_probe(path)[0]['status'], 'unknown')
            root_directory = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o750)
            root_file = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o640, st_size=path.stat().st_size)
            with patch('shiki.collector.Path.lstat', return_value=root_directory), patch('shiki.collector.os.fstat', return_value=root_file):
                data, service = docker_probe({}, {'docker_snapshot_file': str(path)})
                self.assertEqual(data['status'], 'ok')
                self.assertEqual(data['containers'][0]['memory_bytes'], 1234)
                self.assertEqual(service['checked_at'], now)
                with patch('shiki.collector.time.time', return_value=now + 61):
                    old, old_service = docker_snapshot_probe(path)
                self.assertEqual(old['status'], 'unknown')
                self.assertIsNone(old['total'])
                self.assertEqual(old_service['checked_at'], now)
                source = helper.unavailable('error', 'Native daemon not running', now)
                path.write_text(json.dumps(source))
                inactive, inactive_service = docker_snapshot_probe(path)
                self.assertEqual(inactive_service['status'], 'error')
                self.assertIsNone(inactive['total'])
                # A symlink cannot substitute a trusted snapshot even with forged mock metadata.
                link = Path(directory) / 'link.json'
                link.symlink_to(path)
                self.assertEqual(docker_snapshot_probe(link)[0]['status'], 'unknown')
                root_file.st_mode = stat.S_IFREG | 0o660
                self.assertEqual(docker_snapshot_probe(path)[0]['status'], 'unknown')
            for bad_path, age in (('relative.json', 60), ('/run/../tmp/file', 60), (None, 301)):
                with self.assertRaises(ValueError):
                    validate_agent({'device_id': 'node', 'docker_snapshot_file': bad_path,
                                    'docker_snapshot_max_age_seconds': age})

    def test_fixed_helper_inactive_no_docker_and_active_field_whitelist(self):
        with patch.object(helper, 'command', return_value='ActiveState=inactive\nSubState=dead\nMainPID=0\n') as command:
            inactive = helper.collect('/unused-private-config')
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][0], '/usr/bin/systemctl')
        self.assertEqual(inactive['service']['status'], 'error')
        self.assertIsNone(inactive['docker']['total'])
        systemd = 'ActiveState=active\nSubState=running\nMainPID=123\n'
        ps = json.dumps({'ID': 'a' * 64, 'Names': 'web', 'State': 'running', 'Status': 'Up (healthy)',
                         'Command': 'ignored secret argv', 'Labels': 'ignored labels'})
        stats = json.dumps({'ID': 'a' * 64, 'Name': 'web', 'CPUPerc': '105.5%', 'MemUsage': '2MiB / 1GiB'})
        socket_info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFSOCK | 0o660)
        with patch.object(helper, 'command', side_effect=[systemd, ps, stats]) as command, \
             patch('builtins.open', mock_open(read_data='dockerd\n')), \
             patch.object(helper, 'trusted_root_path'), \
             patch.object(helper.Path, 'lstat', return_value=socket_info):
            active = helper.collect('/unused-private-config')
        self.assertEqual(active['docker']['status'], 'ok')
        self.assertEqual(active['docker']['running'], 1)
        self.assertEqual(active['docker']['containers'][0]['cpu_percent'], 105.5)
        self.assertEqual(active['docker']['containers'][0]['memory_bytes'], 2097152)
        self.assertNotIn('ignored', json.dumps(active))
        self.assertNotIn('Command', helper.PS_FORMAT)
        for call in command.call_args_list[1:]:
            self.assertEqual(call.args[0][:3], ['/usr/bin/docker', '--host', 'unix:///run/docker.sock'])
            self.assertNotIn('{{json .}}', call.args[0])
        self.assertEqual(command.call_args_list[2].kwargs['timeout'], 3)

    def test_helper_root_trust_chain_rejects_user_source_and_symlinks(self):
        # The delivered source is user-owned; root execution must use an installed copy.
        with self.assertRaises(ValueError):
            helper.trusted_root_path(Path(helper.__file__).absolute())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.py'
            path.write_text('')
            link = Path(directory) / 'link.py'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                helper.trusted_root_path(link)


if __name__ == '__main__':
    unittest.main()
