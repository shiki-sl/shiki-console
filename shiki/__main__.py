"""Local administration only: pairing, revocation, receiver and read-only agent."""
import argparse
import json
from pathlib import Path
import signal
import sys

from .common import ident, write_private
from .receiver import ReceiverServer, initialize, load_receiver
from .agent import Agent, ConfigLock, agent_initialize, pair_agent


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python3 -m shiki', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'serve', 'pair', 'revoke'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--data-dir', required=True)
        if name in ('pair', 'revoke'):
            cmd.add_argument('--device', required=True)
        if name == 'pair':
            cmd.add_argument('--out', required=True)
            cmd.add_argument('--ttl', type=int, default=300)
    for name in ('agent-init', 'agent-pair', 'agent'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--config', required=True)
        if name == 'agent-init':
            cmd.add_argument('--device', required=True)
        if name == 'agent-pair':
            cmd.add_argument('--url', required=True)
            cmd.add_argument('--receiver', required=True)
            cmd.add_argument('--code-file', required=True)
        if name == 'agent':
            cmd.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            initialize(args.data_dir)
            print('Receiver initialized; viewer token saved privately in viewer.key')
        elif args.command == 'serve':
            config, store = load_receiver(args.data_dir)
            server = ReceiverServer(config, store)
            print('Shiki receiver listening on configured interface', flush=True)
            try:
                server.serve_forever(poll_interval=.3)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.command in ('pair', 'revoke'):
            config, store = load_receiver(args.data_dir)
            device = ident(args.device)
            if device not in {n['id'] for n in config['inventory']}:
                raise ValueError('device not in configured inventory')
            if args.command == 'pair':
                if not 30 <= args.ttl <= 900:
                    raise ValueError('pairing TTL must be 30..900 seconds')
                output = Path(args.out).expanduser().absolute()
                if output.exists():
                    raise ValueError('pairing output exists; choose a fresh output path')
                write_private(output, store.grant(device, args.ttl))
                print('Pairing grant saved privately; expires in ' + str(args.ttl) + ' seconds')
            else:
                store.revoke(device)
                print('Device enrollment and outstanding pairing grant revoked')
        elif args.command == 'agent-init':
            agent_initialize(args.config, args.device)
            print('Agent configuration initialized privately')
        elif args.command == 'agent-pair':
            pair_agent(args.config, args.url, args.receiver, args.code_file)
            print('Device paired with configured receiver')
        else:
            with ConfigLock(Path(args.config).expanduser().absolute()):
                agent = Agent(args.config)
                def stop(_signum, _frame):
                    agent.stop.set()
                signal.signal(signal.SIGTERM, stop)
                signal.signal(signal.SIGINT, stop)
                results = agent.run(once=args.once)
                if args.once:
                    print(json.dumps({'delivery': results}, ensure_ascii=False))
                    return 0 if all(item['status'] == 'ok' for item in results) else 2
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Do not echo arbitrary network/credential-bearing exception details.
        print('Shiki: operation could not complete (' + type(exc).__name__ + '). Check local configuration and target availability.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
