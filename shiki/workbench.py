"""Read-only checks for explicitly enabled, fixed local workbench services."""
import socket
import subprocess
import time


WORKBENCH_SERVICES = {
    'it_tools': ('it-tools.service', False, 8086),
    'it_tools_auth': ('it-tools-auth.service', True, 8844),
}


def configured_services(config):
    names = config.get('monitored_services', [])
    if (not isinstance(names, list) or len(names) > len(WORKBENCH_SERVICES)
            or any(not isinstance(name, str) or name not in WORKBENCH_SERVICES for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('only explicit local workbench services may be monitored')
    return names


def workbench_probe(name):
    unit, user, port = WORKBENCH_SERVICES[name]

    def result(status, detail):
        return {'status': status, 'checked_at': time.time(), 'detail': detail}

    command = ['systemctl'] + (['--user'] if user else [])
    command += ['show', '--property=LoadState,ActiveState,SubState', '--', unit]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, timeout=1.5, check=False)
        if len(completed.stdout) > 4096:
            return result('unknown', unit + ' · 服务状态响应超出限制')
        states = dict(line.split('=', 1) for line in completed.stdout.splitlines() if '=' in line)
        if states.get('LoadState') == 'not-found':
            return result('not_installed', unit + ' · 未安装')
        if completed.returncode or states.get('LoadState') != 'loaded':
            return result('unknown', unit + ' · 无法读取服务状态或权限不足')
        active, sub = states.get('ActiveState'), states.get('SubState')
        if active != 'active' or sub != 'running':
            status = 'error' if active in ('failed', 'inactive') else 'unknown'
            return result(status, unit + ' · ' + str(active) + '/' + str(sub))
    except (OSError, subprocess.TimeoutExpired):
        return result('unknown', unit + ' · 服务状态检查超时或不可用')
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.75):
            pass
    except OSError:
        return result('error', unit + ' · active/running，但本机端口 ' + str(port) + ' 不可达')
    return result('ok', unit + ' · active/running · 本机 TCP ' + str(port)
                  + ' 可达；未验证外部访问或认证会话')
