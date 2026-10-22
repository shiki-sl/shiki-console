import json
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

from shiki.agent import validate_agent
from shiki.common import normalize_snapshot
from shiki.receiver import default_config, device_view, validate_config
from shiki.workbench import configured_services, workbench_probe


class WorkbenchTests(unittest.TestCase):
    def test_only_explicit_fixed_service_names_are_accepted(self):
        self.assertEqual(configured_services({}), [])
        for bad in ('it_tools', ['arbitrary.service'], ['it_tools', 'it_tools'], [{}]):
            with self.assertRaises(ValueError):
                validate_agent({'device_id': '3600', 'monitored_services': bad})
        validate_agent({'device_id': '3600', 'monitored_services': ['it_tools']})

    def test_active_unit_requires_its_local_listener_and_correct_scope(self):
        response = Mock(returncode=0, stdout='LoadState=loaded\nActiveState=active\nSubState=running\n')
        for name, user, port in [('it_tools', False, 8086), ('it_tools_auth', True, 8844)]:
            with patch('shiki.workbench.subprocess.run', return_value=response) as run, \
                    patch('shiki.workbench.socket.create_connection') as connect:
                result = workbench_probe(name)
                self.assertEqual(result['status'], 'ok')
                self.assertIn('未验证外部访问或认证会话', result['detail'])
                self.assertEqual('--user' in run.call_args.args[0], user)
                connect.assert_called_once_with(('127.0.0.1', port), timeout=.75)
            with patch('shiki.workbench.subprocess.run', return_value=response), \
                    patch('shiki.workbench.socket.create_connection', side_effect=ConnectionRefusedError):
                self.assertEqual(workbench_probe(name)['status'], 'error')

    def test_failed_missing_or_unknown_unit_does_not_connect(self):
        cases = [(0, 'LoadState=loaded\nActiveState=failed\nSubState=failed\n', 'error'),
                 (1, 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n', 'not_installed'),
                 (1, '', 'unknown')]
        for code, output, expected in cases:
            with patch('shiki.workbench.subprocess.run', return_value=Mock(returncode=code, stdout=output)), \
                    patch('shiki.workbench.socket.create_connection') as connect:
                self.assertEqual(workbench_probe('it_tools')['status'], expected)
                connect.assert_not_called()
        with patch('shiki.workbench.subprocess.run', side_effect=subprocess.TimeoutExpired('systemctl', 1.5)):
            self.assertEqual(workbench_probe('it_tools_auth')['status'], 'unknown')

    def test_optional_samples_survive_normalization_and_missing_sample_stays_unknown(self):
        sample = normalize_snapshot({})
        self.assertNotIn('it_tools', sample['services'])
        self.assertNotIn('it_tools_auth', sample['services'])
        now = time.time()
        value = {'status': 'ok', 'checked_at': now, 'detail': 'fixed unit and port'}
        normalized = normalize_snapshot({'services': {'it_tools': value, 'arbitrary': value}})
        self.assertEqual(normalized['services']['it_tools'], value)
        self.assertNotIn('arbitrary', normalized['services'])
        config = default_config()
        node = config['inventory'][0]
        node.update(required_services=['it_tools_auth'], required_telemetry=[], update_managed=False)
        validate_config(config)
        row = {'sampled_at': now, 'received_at': now, 'snapshot': json.dumps(sample)}
        view = device_view(node, row, config, now)
        self.assertTrue(any(i['component'] == 'it_tools_auth' and i['severity'] == 'unknown'
                            for i in view['issues']))


if __name__ == '__main__':
    unittest.main()
