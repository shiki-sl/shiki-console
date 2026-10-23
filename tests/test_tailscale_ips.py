"""Self-reported Tailnet addresses survive collection and dashboard normalization."""
import json
import time
import unittest
from unittest.mock import patch

from shiki.collector import tailscale_probe
from shiki.common import normalize_snapshot
from shiki.receiver import default_config, device_view


class TailscaleAddressTests(unittest.TestCase):
    def test_self_addresses_reach_the_dashboard(self):
        addresses = ['100.64.0.10', 'fd7a:115c:a1e0::10']
        status = {'BackendState': 'Running', 'TailscaleIPs': addresses,
                  'Peer': {'other': {'HostName': 'other', 'Online': True,
                                     'TailscaleIPs': ['100.64.0.20']}}}
        with patch('shiki.collector.process_present', return_value=True), \
                patch('shiki.collector.shutil.which', return_value='/usr/bin/tailscale'), \
                patch('shiki.collector.run', return_value=json.dumps(status)) as probe:
            tail, service = tailscale_probe({})
        probe.assert_called_once_with(['tailscale', 'status', '--json'], timeout=1.5)
        sample = normalize_snapshot({'tailscale': tail, 'services': {'tailscale': service}})
        now = time.time()
        config = default_config()
        view = device_view(config['inventory'][0], {'sampled_at': now, 'received_at': now,
                           'snapshot': json.dumps(sample)}, config, now)
        self.assertEqual(view['snapshot']['tailscale']['ips'], addresses)
        self.assertNotIn('100.64.0.20', view['snapshot']['tailscale']['ips'])

    def test_old_samples_and_unavailable_probes_do_not_invent_addresses(self):
        self.assertEqual(normalize_snapshot({'tailscale': {'status': 'ok'}})['tailscale']['ips'], [])
        tail, service = tailscale_probe({})
        self.assertEqual(tail['ips'], [])
        self.assertEqual(service['status'], 'unknown')
        with self.assertRaises(ValueError):
            normalize_snapshot({'tailscale': {'ips': ['100.64.0.10', '<script>']}})


if __name__ == '__main__':
    unittest.main()
