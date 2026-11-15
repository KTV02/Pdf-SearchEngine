#!/usr/bin/env python3
"""Remember private local values as salted fingerprints, never as plaintext."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from privacy_check import git_path, normalize


def remember(values):
    path = git_path('privacy/private-fingerprints.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        profile = json.loads(path.read_text())
    else:
        profile = {'version': 1, 'salt': secrets.token_hex(32), 'fingerprints': []}
    hashes = set(profile['fingerprints'])
    hashes.update(hashlib.sha256((profile['salt'] + normalize(v)).encode()).hexdigest()
                  for v in values if len(v.strip()) >= 8)
    profile['fingerprints'] = sorted(hashes)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(profile, f)
            f.write('\n')
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()
    return len(hashes)


def refresh_local():
    """Read only this checkout's local settings and labeled runtime mounts."""
    import io
    import re
    import shutil
    import subprocess
    import tarfile
    from privacy_check import ROOT, git
    values = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                collect(str(key))
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str) and len(value.strip()) >= 8:
            values.add(value.strip())

    for filename in ['.env', 'app/.env']:
        path = ROOT / filename
        if path.is_file():
            for line in path.read_text().splitlines():
                match = re.match(r'\s*(?:export\s+)?[A-Za-z_][\w]*\s*=\s*(.*)', line)
                if match:
                    collect(match.group(1).strip().strip("\"'"))
    for path in [ROOT / 'app/backend/access_config.json', ROOT / 'app/backend/roles.json',
                 *(ROOT / 'app/email_configs').glob('*.json')]:
        if path.is_file():
            collect(json.loads(path.read_text()))
    key = ROOT / 'app/.fernet_key'
    if key.is_file():
        collect(key.read_text())

    def command(args):
        return subprocess.run(args, capture_output=True, timeout=60)

    mounts = {ROOT / part for part in ['app/data', 'app/cloud', 'app/ocr']}
    if shutil.which('docker'):
        result = command(['docker', 'ps', '-aq'])
        ids = result.stdout.decode().split() if result.returncode == 0 else []
        if ids:
            result = command(['docker', 'inspect', *ids])
            if result.returncode:
                raise ValueError()
            for container in json.loads(result.stdout):
                labels = container.get('Config', {}).get('Labels') or {}
                wd = labels.get('com.docker.compose.project.working_dir', '')
                if wd != str(ROOT) and not wd.startswith(str(ROOT) + '/'):
                    continue
                env = dict(x.split('=', 1) for x in container.get('Config', {}).get('Env', [])
                           if '=' in x)
                collect(env)
                for mount in container.get('Mounts', []):
                    if mount.get('Type') == 'bind' and mount.get('Destination') in [
                            '/app/ocr', '/app/cloud', '/app/backups', '/app/index_folder']:
                        mounts.add(Path(mount['Source']))
                if labels.get('com.docker.compose.service') != 'backend':
                    continue
                files = ['/app/index_folder/cloud_connections.json',
                         '/app/index_folder/cloud_provider_config.json',
                         '/app/index_folder/storage_config.json']
                files += [v for k, v in env.items() if k.endswith('_TOKEN_FILE')
                          and v.startswith('/run/secrets/') and any(
                          m.get('Destination') == v for m in container.get('Mounts', []))]
                for filename in files:
                    result = command(['docker', 'cp', container['Id'] + ':' + filename, '-'])
                    if result.returncode:
                        continue
                    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
                        for member in archive:
                            if not member.isfile() or member.size > 8 * 1024 * 1024:
                                continue
                            data = archive.extractfile(member).read().decode()
                            if filename.endswith('.json'):
                                collect(json.loads(data))
                            else:
                                collect(data)
    for folder in mounts:
        for base, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = [d for d in dirs if d != '.git']
            for filename in files:
                if Path(filename).suffix.lower() in {'.pdf', '.docx', '.xlsx', '.eml'}:
                    collect(filename)
    # Already-reviewed public placeholders must not become private deny entries.
    public = []
    policy = json.loads(git('show', 'HEAD:.privacy-policy.json').stdout)
    baseline = policy['reviewed_placeholder_commit']
    # A fixed audited snapshot prevents a later accidental commit from teaching
    # the local guard that a newly exposed private value is a safe placeholder.
    objects = {row.split(b'\t', 1)[0].split()[2].decode() for row in
               git('ls-tree', '-rz', baseline).stdout.split(b'\0') if row}
    for obj in objects:
        public.append(git('cat-file', 'blob', obj).stdout.decode(errors='replace'))
    public_text = normalize('\n'.join(public))
    return remember(v for v in values if normalize(v) not in public_text)


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--refresh-local']:
            count = refresh_local()
        elif not sys.argv[1:]:
            # Input is a JSON array over stdin; never put secrets in arguments/history.
            values = json.load(sys.stdin)
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError()
            count = remember(values)
        else:
            raise ValueError()
        print(f'Local private-value profile updated ({count} fingerprints; no plaintext stored).')
    except Exception:
        print('Profile update failed; private values and raw errors suppressed.', file=sys.stderr)
        sys.exit(1)
