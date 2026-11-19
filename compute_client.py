"""Submit and inspect registered compute jobs, or call the worker protocol."""
import argparse
import json
from pathlib import Path

from coordination_client import ControlDenied, device_client
from handoff import limited


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('local-config.json'))
    parser.add_argument('--project', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    status = commands.add_parser('status')
    status.add_argument('job')
    for name in ('submit', 'heartbeat', 'claim', 'event'):
        command = commands.add_parser(name)
        if name == 'event':
            command.add_argument('job')
        command.add_argument('--body', type=Path, required=True, help='UTF-8 JSON request file')
    args = parser.parse_args()
    try:
        client = device_client(json.loads(limited(args.config, 2 * 1024**2).decode('utf-8-sig')))
        if client is None:
            raise ValueError('计算作业需要设备协调配置')
        endpoints = {'submit': 'jobs', 'heartbeat': 'heartbeat', 'claim': 'claim'}
        if args.command in ('status', 'event'):
            from uuid import UUID

            job = str(UUID(args.job))
            endpoint = f'jobs/{job}' + ('/events' if args.command == 'event' else '')
        else:
            endpoint = endpoints[args.command]
        body = None if args.command == 'status' else json.loads(limited(args.body, 65536).decode('utf-8-sig'))
        if args.command != 'status' and not isinstance(body, dict):
            raise ValueError('请求内容需要 JSON 对象')
        print(json.dumps(client.request(args.project, 'compute/' + endpoint, body)))
    except (OSError, ValueError, KeyError, TypeError, ControlDenied) as error:
        parser.exit(1, f'计算作业请求失败：{getattr(error, "detail", None) or error}\n')


if __name__ == '__main__':
    main()
