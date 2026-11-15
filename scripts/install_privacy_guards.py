#!/usr/bin/env python3
"""Install a checksum-pinned local scanner and opt-in Git hooks."""
import argparse
import hashlib
import io
import json
import os
import platform
from pathlib import Path
import ssl
import subprocess
import sys
import tarfile
import urllib.request
from privacy_check import ROOT, git, git_path

VERSION = '8.30.1'
CHECKSUMS = {
    "gitleaks_8.30.1_darwin_arm64.tar.gz": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    "gitleaks_8.30.1_darwin_x64.tar.gz": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    "gitleaks_8.30.1_linux_arm64.tar.gz": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    "gitleaks_8.30.1_linux_armv7.tar.gz": "8d39f0d94ba0d774b2282187656fb039a2d82893ec1fd6be7d7121aae759a57d",
    "gitleaks_8.30.1_linux_x64.tar.gz": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
}



def configure_hooks():
    hooks = git_path('privacy-hooks').resolve()
    old = git('config', '--get', 'core.hooksPath', check=False).stdout.decode().strip()
    if old and old not in {'.githooks', str(hooks)}:
        raise RuntimeError('Existing custom hooks were preserved; integrate the privacy hooks manually.')
    default = git_path('hooks')
    if not old and default.exists() and any(p.is_file() and not p.name.endswith('.sample')
            and os.access(p, os.X_OK) for p in default.iterdir()):
        raise RuntimeError('Existing active hooks were preserved; integrate the privacy hooks manually.')
    hooks.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ['pre-commit', 'commit-msg', 'pre-push']:
        source = ROOT / '.githooks' / name
        target = hooks / name
        fd = os.open(target.with_suffix('.tmp'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, 'wb') as f:
            f.write(source.read_bytes())
        os.replace(target.with_suffix('.tmp'), target)
        os.chmod(target, 0o700)
    # Git-local absolute entry points survive checkouts without versioned hooks.
    git('config', '--local', 'core.hooksPath', str(hooks))


def install(scanner_only=False):
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x64',
            'amd64': 'x64', 'armv7l': 'armv7'}.get(machine)
    name = f'gitleaks_{VERSION}_{system}_{arch}.tar.gz'
    if name not in CHECKSUMS:
        raise RuntimeError('Unsupported scanner platform; no hooks were installed.')
    path = git_path('privacy-tools/gitleaks')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Verify the binary by comparing it to the checksum-verified release payload.
    context = ssl.create_default_context()
    if system == 'darwin' and Path('/etc/ssl/cert.pem').exists():
        context = ssl.create_default_context(cafile='/etc/ssl/cert.pem')
    url = f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/{name}'
    with urllib.request.urlopen(url, timeout=60, context=context) as response:
        data = response.read(64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != CHECKSUMS[name]:
        raise RuntimeError('Scanner checksum mismatch; no hooks were installed.')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmember('gitleaks')
        if not member.isfile():
            raise RuntimeError('Invalid scanner release.')
        binary = archive.extractfile(member).read()
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(binary)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    if not scanner_only:
        configure_hooks()
    print('Pinned scanner installed' + ('.' if scanner_only else '; repository privacy hooks enabled.'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scanner-only', action='store_true')
    args = parser.parse_args()
    try:
        install(args.scanner_only)
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else 'Installation failed; raw details suppressed.'
        print(message, file=sys.stderr)
        sys.exit(1)
