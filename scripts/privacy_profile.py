#!/usr/bin/env python3
"""Remember private local values as salted fingerprints, never as plaintext."""
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sys
from privacy_check import canonical, git_path, normalize


DOCUMENT = re.compile(r'\.(pdf|docx?|xlsx?|eml|msg)$', re.I)
GENERIC = {'localhost', '127.0.0.1', '0.0.0.0', '::1'}


def remember(values, min_length=8, exclude_text=None):
    path = git_path('privacy/private-fingerprints.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        profile = json.loads(path.read_text())
    else:
        profile = {'version': 1, 'salt': secrets.token_hex(32), 'fingerprints': []}
    hashes = set(profile['fingerprints'])
    expanded, plain_stems = set(), set()
    for v in values:
        v = v.strip()
        expanded.add(v)
        # Individual list members, URL hosts, e-mail addresses, and document stems.
        parts = [p.strip('"\'') for p in re.split(r'[,;]\s*', v)]
        if len(parts) == 1 and not DOCUMENT.search(v):
            spaced = v.split()
            # Whitespace-separated lists of addresses/URLs/tokens, never prose or document names.
            if len(spaced) > 1 and all(re.search(r'[@:/]|\d', p) for p in spaced):
                parts = spaced
        expanded.update(p for p in parts if len(p) >= 8)
        expanded.update(re.findall(r'[a-z][a-z0-9+.-]*://([^/:\s@]+)', v, re.I))
        expanded.update(re.findall(r'[\w.%+-]+@[\w-]+\.[\w.-]+', v))
        if DOCUMENT.search(v):
            stem = v.rsplit('.', 1)[0]
            # A single ordinary word is too common in prose to block; the full file name
            # stays protected, and multi-word or numbered stems are kept.
            if len(stem.split()) > 1 or re.search(r'[\d_\-]', stem):
                expanded.add(stem)
            else:
                plain_stems.add(normalize(stem))
    forms = set()
    for v in expanded:
        if len(v.strip()) < min_length:
            continue
        forms.add(normalize(v))
        joined = canonical(v)
        if len(joined) >= min_length and ' ' in joined:
            forms.add(joined)  # punctuation/spacing variants in prose
    if exclude_text is not None:
        # Reviewed public placeholders and generic words must not become deny entries;
        # drop any such entries an earlier refresh may have stored.
        word_parts = {normalize(p) for v in expanded for p in re.split(r'[,;\s]+', v) if len(p) >= 5}
        canonical_public = ' ' + canonical(exclude_text) + ' '
        public_forms = {f for f in forms | word_parts | {canonical(p) for p in word_parts}
                        if f in exclude_text or f in GENERIC or ' ' + f + ' ' in canonical_public}
        # Single words split out of longer values are not stored by this version; remove
        # any left by an earlier refresh so ordinary words do not block publication.
        public_forms |= (word_parts | plain_stems) - {normalize(v) for v in expanded}
        forms -= public_forms
        hashes -= {hashlib.sha256((profile['salt'] + f).encode()).hexdigest() for f in public_forms}
    hashes.update(hashlib.sha256((profile['salt'] + f).encode()).hexdigest() for f in forms)
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

    # Environment files, Compose overrides, and local start scripts (KEY=value / KEY: value).
    local_settings = ['.env', 'app/.env', 'compose.override.yaml', 'compose.override.yml',
                      'docker-compose.override.yml', 'docker-compose.override.yaml',
                      'app/compose.override.yaml', 'app/compose.override.yml',
                      'app/docker-compose.override.yml', 'app/docker-compose.override.yaml',
                      'app/start.sh']
    for filename in local_settings:
        path = ROOT / filename
        if path.is_file():
            for line in path.read_text(errors='replace').splitlines():
                match = re.match(r'\s*(?:-\s*)?(?:export\s+)?[A-Za-z_][\w]*\s*[=:]\s*(.+)', line)
                if match:
                    collect(match.group(1).split(' #')[0].strip().strip("\"'"))
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
    index_roots = {ROOT / 'app/data/index'}
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
                        if mount.get('Destination') == '/app/index_folder':
                            index_roots.add(Path(mount['Source']))
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
    index_names = set()
    for folder in mounts | index_roots:
        if folder in index_roots:
            try:
                index_names.update(p.name for p in folder.iterdir() if p.is_dir() and len(p.name) >= 6)
            except OSError:
                pass
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
    remember(values, exclude_text=public_text)
    # Index names are explicit private identifiers, so shorter values are kept too.
    return remember(index_names, min_length=6, exclude_text=public_text)


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
