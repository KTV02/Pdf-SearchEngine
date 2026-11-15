#!/usr/bin/env python3
"""Test actual Docker ignore behavior using public staged source and synthetic data."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from privacy_check import ROOT, git, path_problem, snapshot


def main():
    policy = json.loads(git('show', ':0:.privacy-policy.json').stdout)
    entries = snapshot(staged=True)
    for path, mode, _ in entries:
        if path_problem(path, policy, mode):
            raise RuntimeError('Review the staged public paths before running context tests.')
    context = subprocess.run(['docker', 'context', 'inspect'], capture_output=True)
    hosts = [x.get('Endpoints', {}).get('docker', {}).get('Host', '')
             for x in json.loads(context.stdout)]
    if len(hosts) != 1 or not hosts[0].startswith('unix://'):
        raise RuntimeError('Use a local Unix-socket Docker engine for these disposable builds.')
    names = {p for p, _, _ in entries}
    expected = {
        'root': set(),
        'backend': {p.removeprefix('app/backend/') for p in names
                    if re.fullmatch(r'app/backend/[^/]+\.py', p)} | {'requirements.security.lock'},
        'mail': {p.removeprefix('app/email_processor/') for p in names
                 if re.fullmatch(r'app/email_processor/[^/]+\.py', p)} | {'requirements.security.lock'},
        'test': {p.removeprefix('app/') for p in names
                 if re.fullmatch(r'app/(?:backend|email_processor|tests)/[^/]+\.py', p)} |
                {'backend/requirements.txt', 'email_processor/requirements.txt',
                 'tests/requirements.txt', 'docker-compose.yml', 'pytest.ini'},
    }
    with tempfile.TemporaryDirectory(prefix='public-context-tests-') as td:
        base = Path(td)
        src = base / 'source'
        for path, _, obj in entries:
            target = src / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git('cat-file', 'blob', obj).stdout)
        # None of these files contains real private data; do not copy the real worktree.
        sentinels = ['app/.env', 'app/backend/access_config.json', 'app/backend/roles.json',
                     'app/backend/local_private.py', 'app/email_processor/local_private.py',
                     'app/tests/private_extra.py', 'app/cloud/synthetic-document.pdf',
                     'app/data/synthetic-backup.zip', '.git/private-fingerprint.json']
        for path in sentinels:
            target = src / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('SYNTHETIC_PRIVATE_SENTINEL')
        for label, rel in [('root', '.'), ('backend', 'app/backend'),
                           ('mail', 'app/email_processor'), ('test', 'app')]:
            out = base / ('result-' + label)
            result = subprocess.run(['docker', '--host', hosts[0], 'buildx', 'build',
                '--builder', 'default', '--progress=quiet', '--no-cache', '--file', '-',
                '--output', 'type=local,dest=' + str(out), str(src / rel)],
                input=b'FROM scratch\nCOPY . /out\n', capture_output=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f'{label}: disposable context test failed; raw builder output suppressed.')
            actual = {p.relative_to(out / 'out').as_posix() for p in (out / 'out').rglob('*')
                      if p.is_file()}
            if actual != expected[label]:
                raise RuntimeError(f'{label}: unexpected build input; private filenames suppressed.')
            print(f'{label}: only {len(actual)} reviewed source inputs exported; private sentinels excluded.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else
              'Context test failed; raw details suppressed.', file=sys.stderr)
        sys.exit(1)
