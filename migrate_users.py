"""Offline migration of a legacy installation to an explicitly selected account."""
import argparse
import json
from pathlib import Path
import shutil
import sys

from machine_settings import atomic_json
from paths import DATA
from web_auth import Auth


def import_legacy(auth, username, password):
    source = auth.root
    marker = source / '.runtime' / 'legacy-owner.json'
    owner = None
    if marker.exists():
        owner = json.loads(marker.read_text())
        account = auth.find(username)
        if not account or account['id'] != owner['user_id']:
            raise ValueError('旧数据已迁移到另一个账号，拒绝重复导入')
        auth.login(dict(username=username, password=password))
        if owner.get('state', 'ready') == 'ready':
            return account['id']
    config = json.loads((source / 'config.json').read_text())
    credentials = set()
    for host in config['hosts']:
        relative = Path(host['credentials_file'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('请先将旧凭据路径改为数据目录内的相对路径')
        path = (source / relative).resolve()
        path.relative_to(source.resolve())
        if not path.is_file():
            raise ValueError('旧配置引用的凭据文件不存在')
        credentials.add(relative)
    account = auth.find(username)
    if account:
        auth.login(dict(username=username, password=password))
        user_id = account['id']
    else:
        user_id = auth.register(dict(username=username, password=password))
    target = source / 'users' / user_id
    current = json.loads((target / 'config.json').read_text())
    if current['hosts'] and not (owner and owner.get('state') == 'importing'):
        raise ValueError('目标账号已有机器，拒绝覆盖；请检查迁移是否中断')
    atomic_json(marker, dict(user_id=user_id, state='importing'))
    paths = set(credentials)
    for name in ('.runtime/known_hosts', '.runtime/events.jsonl'):
        if (source / name).exists():
            paths.add(Path(name))
    for relative in paths:
        destination = target / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
        destination.chmod(0o600)
    # Publish configuration last, after all credentials and operation records exist.
    atomic_json(target / 'config.json', config)
    atomic_json(marker, dict(user_id=user_id, state='ready', hosts=len(config['hosts'])))
    (source / '.runtime/setup-token').unlink(missing_ok=True)
    return user_id


def main():
    parser = argparse.ArgumentParser(description='将旧机器数据迁移到指定平台账号（先停止服务并备份）')
    parser.add_argument('--username', required=True)
    parser.add_argument('--password-stdin', required=True, action='store_true')
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip('\r\n')
    auth = Auth(DATA / '.runtime')
    user_id = import_legacy(auth, args.username, password)
    print(f'迁移完成：{args.username}；用户 ID：{user_id}')


if __name__ == '__main__':
    main()
