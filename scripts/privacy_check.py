#!/usr/bin/env python3
"""Check public source snapshots without printing matched values or private names."""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
import warnings

def _repository_root():
    """Run from the checkout's scripts/ or as the reviewed copy installed in Git's metadata."""
    here = Path(__file__).resolve().parent
    if here.name == 'scripts' and (here.parent / '.privacy-policy.json').exists():
        return here.parent
    result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True)
    return Path(result.stdout.decode().strip()) if result.returncode == 0 else Path.cwd()


ROOT = _repository_root()
GUARD_FILES = ['scripts/privacy_check.py', '.privacy-policy.json', '.gitleaks.toml',
               '.githooks/pre-commit', '.githooks/commit-msg', '.githooks/pre-push']
ZERO = '0' * 40
POLICY = ROOT / '.privacy-policy.json'
MAX_FILE = 2 * 1024 * 1024
PRIVATE_DIRS = {'data', 'cloud', 'ocr', 'index_folder', 'tmp', 'logs', 'backups',
                'email_configs', 'processor_state', 'downloads', 'uploads', 'other',
                '.local', '.private', '.git', '.ssh', '__pycache__'}
PRIVATE_NAMES = {'access_config.json', 'roles.json', 'role_indices.json',
                 'storage_config.json', 'cloud_connections.json',
                 'cloud_provider_config.json', 'settings.js', '.fernet_key',
                 'compose.override.yml', 'compose.override.yaml',
                 'docker-compose.override.yml', 'docker-compose.override.yaml',
                 'start.sh'}
PRIVATE_SUFFIXES = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.eml', '.msg', '.db',
                    '.sqlite', '.sqlite3', '.log', '.bak', '.backup', '.old', '.orig',
                    '.save', '.pem', '.key', '.p12', '.pfx', '.zip', '.gz', '.tgz',
                    '.tar', '.7z', '.pyc', '.swp', '.swo'}
EMAIL = re.compile(r'[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}')
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
IPV6 = re.compile(r'(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:]+(?:%\w+)?')
MACHINE = re.compile(r'/(?:home|Users|mnt|media)/[^\s\'"`<>),]+')
HOST = re.compile(r'\b[\w-]+\.(?:[\w-]+\.)*(?:ts\.net|local|lan|internal|duckdns\.org|ddns\.net)\b', re.I)
PROVIDER = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{35,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{15,})\b')
DOC_LITERAL = re.compile(r'[\w .()\-]{5,200}\.(?:pdf|docx|xlsx|eml)\b', re.I)


class Blocked(Exception):
    pass


def git(*args, input=None, check=True):
    result = subprocess.run(['git', '-C', str(ROOT), *args], input=input,
                            capture_output=True, env=dict(os.environ,
                            GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))
    if check and result.returncode:
        raise Blocked('Git inspection failed; no raw Git output was displayed.')
    return result


def git_path(name):
    """Guard state lives in the common Git directory, shared by linked worktrees."""
    p = Path(git('rev-parse', '--git-common-dir').stdout.decode().strip())
    return (p if p.is_absolute() else ROOT / p) / name


def glob_regex(pattern):
    """Slash-aware globs; **/ also matches zero directory levels."""
    out, i = '', 0
    while i < len(pattern):
        if pattern[i:i + 3] == '**/':
            out += '(?:.*/)?'; i += 3
        elif pattern[i:i + 2] == '**':
            out += '.*'; i += 2
        elif pattern[i] == '*':
            out += '[^/]*'; i += 1
        elif pattern[i] == '?':
            out += '[^/]'; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile('^' + out + '$')


def path_problem(path, policy, mode='100644'):
    p = PurePosixPath(path)
    if mode not in {'100644', '100755'}:
        return 'symlinks and submodules require explicit privacy review'
    if p.is_absolute() or '..' in p.parts or '\\' in path:
        return 'unsafe path'
    if any(part.lower() in PRIVATE_DIRS for part in p.parts[:-1]):
        return 'runtime/private directory'
    n = p.name.lower()
    if (n in PRIVATE_NAMES or any(n.startswith(base) for base in [
            'access_config.json', 'roles.json', 'role_indices.json', 'storage_config.json',
            'cloud_connections.json', 'cloud_provider_config.json', '.fernet_key', 'settings.js'])
            or n in {'local_settings.py', 'local_config.py', 'private_settings.py', 'private_config.py'}
            or (n.startswith('.env') and n != '.env.example')):
        return 'private configuration'
    if p.suffix.lower() in PRIVATE_SUFFIXES or n.endswith('~'):
        return 'document, archive, credential, or generated output'
    if any(x in n for x in ['.local.', '.private.', 'compose.resolved.',
                             'compose.rendered.', 'docker-inspect.']):
        return 'local configuration or diagnostic output'
    if not any(glob_regex(x).fullmatch(path) for x in policy['public_paths']):
        return 'path outside reviewed public source locations'
    return None


def example_email(value):
    if value.lower() in {'git@github.com', 'noreply@anthropic.com'}:
        return True  # GitHub SSH transport and the public co-author trailer address.
    domain = value.rsplit('@', 1)[1].lower()
    return (domain in {'example.com', 'example.org', 'example.net', 'example.test',
                       'users.noreply.github.com', 'noreply.github.com'} or
            domain.endswith(('.example', '.test', '.invalid')))


def private_ip(value):
    try:
        ip = ipaddress.ip_address(value.split('%')[0])
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified:
        return False
    allowed = ['192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '2001:db8::/32']
    return not any(ip.version == (net := ipaddress.ip_network(n)).version and ip in net
                   for n in allowed)


def normalize(value):
    return unicodedata.normalize('NFC', value).strip().casefold()


WORD = re.compile(r'[^\W_]+(?:[.@+%~\-][^\W_]+)*')
DELIMITED = re.compile(r'[^\s=:;,"\'`()<>\[\]{}|]{4,2048}')
BASE64ISH = re.compile(r'[A-Za-z0-9+/_\-]{12,}={0,2}')


def canonical(value):
    """Words of a value joined by single spaces, so punctuation/spacing variants match."""
    return ' '.join(WORD.findall(normalize(value)))


def candidates(text):
    """Candidates for local salted fingerprints, including decoded and normalized forms."""
    text = unicodedata.normalize('NFC', text)
    variants = {text}
    for decode in (urllib.parse.unquote, html.unescape):
        try:
            variants.add(unicodedata.normalize('NFC', decode(text)))
        except Exception:
            pass
    for match in BASE64ISH.finditer(text):
        raw = match.group()
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                decoded = decoder(raw + '=' * (-len(raw) % 4)).decode('utf-8')
            except Exception:
                continue
            if decoded.isprintable():
                variants.add(unicodedata.normalize('NFC', decoded))
    for variant in variants:
        yield from _candidates(variant)


def _candidates(text):
    yield text.strip()
    for line in text.splitlines():
        yield line.strip()
        yield line.strip().lstrip('#/*;-> ').strip()
    for match in DELIMITED.finditer(text):
        yield match.group()
        yield match.group().rsplit('/', 1)[-1]
    words = WORD.findall(text)
    for start in range(len(words)):
        for size in range(1, 9):
            if start + size > len(words):
                break
            yield ' '.join(words[start:start + size])
    for match in re.finditer(r'[\w@.+/=:,%!~\-]{4,2048}', text):
        yield match.group()
        yield match.group().rsplit('/', 1)[-1]
    for match in re.finditer(r'''["']([^"'\n]{4,2048})["']''', text):
        value = match.group(1)
        yield value
        yield value.rsplit('/', 1)[-1]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                decoded = ast.literal_eval(match.group())
            if isinstance(decoded, str):
                yield decoded
        except (ValueError, SyntaxError):
            pass
    for match in DOC_LITERAL.finditer(text):
        # Include tails so prose before an unquoted document name cannot hide it.
        words = match.group().strip().split()
        for start in range(len(words)):
            yield ' '.join(words[start:])


def load_profile(required=False):
    p = git_path('privacy/private-fingerprints.json')
    if not p.exists():
        if required:
            raise Blocked('The local private-value profile is missing; create it with '
                          'scripts/privacy_profile.py or explicitly opt out with '
                          'git config privacy.allowMissingProfile true.')
        return None
    try:
        profile = json.loads(p.read_text())
        return profile['salt'], set(profile['fingerprints'])
    except (ValueError, KeyError, OSError):
        raise Blocked('The local private-value profile is unreadable; repair it before publishing.')


def reviewed_public_forms(policy):
    """Values the owner deliberately published (e.g. the copyright holder) and their words."""
    forms = set()
    for value in policy.get('reviewed_public_values', []):
        forms.update({normalize(value), canonical(value)})
        forms.update(word for word in canonical(value).split() if len(word) >= 5)
    return forms


def without_public_values(profile, policy):
    if not profile:
        return profile
    salt, hashes = profile
    allowed = {hashlib.sha256((salt + form).encode()).hexdigest() for form in reviewed_public_forms(policy)}
    return salt, hashes - allowed


def content_problems(data, policy, blob='', profile=None):
    if len(data) > MAX_FILE:
        return [('oversized source file', 0)]
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return [('binary/non-UTF-8 content requires privacy review', 0)]
    if '\x00' in text:
        return [('binary content requires privacy review', 0)]
    exceptions = {(x['rule'], x['fingerprint']) for x in policy.get('historical_content_exceptions', [])
                  if x['blob'] == blob}
    findings = set()
    for rule, regex, predicate in [
        ('personal-email', EMAIL, lambda x: not example_email(x)),
        ('network-address', IPV4, private_ip),
        ('network-address', IPV6, private_ip),
        ('machine-path', MACHINE, lambda x: True),
        ('private-hostname', HOST, lambda x: not x.endswith(('.example', '.invalid'))),
        ('credential-pattern', PROVIDER, lambda x: True),
    ]:
        for m in regex.finditer(text):
            if predicate(m.group()) and (rule, hashlib.sha256(m.group().encode()).hexdigest()) not in exceptions:
                findings.add((rule, text.count('\n', 0, m.start()) + 1))
    if profile:
        salt, hashes = profile
        checked = set()
        for value in candidates(text):
            value = normalize(value)
            if len(value) < 5 or value in checked:
                continue
            checked.add(value)
            if hashlib.sha256((salt + value).encode()).hexdigest() in hashes:
                findings.add(('known-private-local-value', 0))
                break
    return sorted(findings)


def check_identity(name, email, policy):
    return name in policy['public_author_names'] and email.lower().endswith('@users.noreply.github.com')


def snapshot(staged=False, commit=None):
    args = ('ls-files', '--stage', '-z') if staged else ('ls-tree', '-rz', commit)
    entries = []
    for row in git(*args).stdout.split(b'\0'):
        if not row:
            continue
        meta, path = row.split(b'\t', 1)
        mode, objtype, obj = meta.split() if not staged else (None, None, None)
        if staged:
            mode, obj, stage = meta.split()
            if stage != b'0':
                raise Blocked('Resolve index conflicts before privacy checking.')
        entries.append((path.decode('utf-8'), mode.decode(), obj.decode()))
    return entries


def blob_bytes(obj):
    return git('cat-file', 'blob', obj).stdout


def check_build_inputs(entries, read):
    """Validate deliberately restricted Docker allowlists against the Git snapshot."""
    names = {p for p, _, _ in entries}
    expected = {
        '.dockerignore': set(),
        'app/backend/.dockerignore': {p.removeprefix('app/backend/') for p in names
            if re.fullmatch(r'app/backend/[^/]+\.py', p)} | {'requirements.security.lock'},
        'app/email_processor/.dockerignore': {p.removeprefix('app/email_processor/') for p in names
            if re.fullmatch(r'app/email_processor/[^/]+\.py', p)} | {'requirements.security.lock'},
        'app/.dockerignore': {p.removeprefix('app/') for p in names
            if re.fullmatch(r'app/(?:backend|email_processor|tests)/[^/]+\.py', p)} |
            {'backend/', 'email_processor/', 'tests/', 'backend/requirements.txt',
             'email_processor/requirements.txt', 'tests/requirements.txt',
             'docker-compose.yml', 'compose.integration.yml', 'pytest.ini'},
    }
    for path, allowed in expected.items():
        if path not in names:
            raise Blocked('A required Docker exclusion file is missing.')
        rules = [s.strip() for s in read(path).decode().splitlines()
                 if s.strip() and not s.lstrip().startswith('#')]
        prefix = ['**']
        if path == 'app/.dockerignore':
            prefix += ['!backend/', 'backend/**', '!email_processor/',
                       'email_processor/**', '!tests/', 'tests/**']
            allowed -= {'backend/', 'email_processor/', 'tests/'}
        rest = rules[len(prefix):]
        if (rules[:len(prefix)] != prefix or set(rest) != {'!' + p for p in allowed}
                or len(rest) != len(allowed)):
            raise Blocked('Docker input allowlists need review; update explicit source inputs when adding modules.')



def reference_messages(policy, profile):
    messages = []
    seen = set()
    raw = git('for-each-ref', '--format=%(refname)%00%(objectname)%00%(objecttype)').stdout
    for row in raw.splitlines():
        name, obj, kind = row.decode().split('\0')
        if content_problems(name.encode(), policy, profile=profile):
            raise Blocked('A reference name contains potentially private content; name suppressed.')
        messages.append(name.encode())
        while kind == 'tag' and obj not in seen:
            seen.add(obj)
            header, _, message = git('cat-file', 'tag', obj).stdout.decode().partition('\n\n')
            identity = next((line[7:] for line in header.splitlines()
                             if line.startswith('tagger ')), '')
            match = re.match(r'(.*) <([^>]+)>', identity)
            if not match or not check_identity(*match.groups(), policy):
                raise Blocked('History contains an unreviewed tag identity; identity values suppressed.')
            if content_problems(message.encode(), policy, profile=profile):
                raise Blocked('A tag message contains potentially private content; values suppressed.')
            messages.append(message.encode())
            obj = next(line[7:] for line in header.splitlines() if line.startswith('object '))
            kind = next(line[5:] for line in header.splitlines() if line.startswith('type '))
    return messages


def file_digest(data):
    return hashlib.sha256(data).hexdigest()


def verify_reviewed_guards(revisions):
    """Hooks run a reviewed copy; block if committed/staged guard files differ from it."""
    path = git_path('privacy-hooks/reviewed.json')
    try:
        reviewed = json.loads(path.read_text())
    except (OSError, ValueError):
        raise Blocked('Reinstall the reviewed privacy guards: python3 scripts/install_privacy_guards.py')
    for revision in revisions:
        for name in GUARD_FILES:
            result = git('show', f'{revision}:{name}', check=False)
            if result.returncode:
                raise Blocked('Privacy guard is unavailable on this branch; restore reviewed guards '
                              'before committing or pushing.')
            if file_digest(result.stdout) != reviewed.get(name):
                raise Blocked('Guard, policy, or scanner files differ from the locally reviewed copy. '
                              'Review the change, then rerun python3 scripts/install_privacy_guards.py.')


def pushed_updates(stream):
    """Parse pre-push input: the exact commits and destination names being sent."""
    tips, names = [], []
    for line in stream.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, _ = parts
        names += [local_ref, remote_ref]
        if local_sha != ZERO:
            tips.append(local_sha)
    return tips, names


def local_build_overrides():
    """Untracked Dockerfile-specific ignore files would replace the reviewed allowlists."""
    tracked = set(git('ls-files', '-z').stdout.decode().split('\0'))
    found = 0
    for folder in ['app', 'app/backend', 'app/email_processor', 'app/tests']:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for item in base.iterdir():
            rel = f'{folder}/{item.name}'
            if item.name.endswith('.dockerignore') and item.name != '.dockerignore' and rel not in tracked:
                found += 1
    if found:
        raise Blocked('An untracked Dockerfile-specific .dockerignore would override the reviewed '
                      'build allowlists; remove it or review it as public source.')


def scanner_path():
    path = git_path('privacy-tools/gitleaks')
    if not path.is_file():
        raise Blocked('Install the pinned scanner first: python3 scripts/install_privacy_guards.py')
    return path


def run_scanner(mode, data=None, log_options=None):
    scanner = scanner_path()
    with tempfile.TemporaryDirectory(prefix='pdf-publication-') as td:
        report = Path(td) / 'findings.json'
        args = [str(scanner), mode, '--no-banner', '--no-color', '--redact=100',
                '--config', str(ROOT / '.gitleaks.toml'), '--report-format=json',
                '--report-path=' + str(report)]
        if mode == 'git':
            args += [str(ROOT), '--log-opts=' + (log_options or '--all')]
        result = subprocess.run(args, input=data, capture_output=True)
        if result.returncode == 1:
            raise Blocked('Credential scanner blocked publication. Matched values and raw output were suppressed.')
        if result.returncode != 0:
            raise Blocked('Credential scanner could not complete; raw output was suppressed.')


def inspect_snapshot(entries, policy, seen, profile, stage_bytes=None):
    objects = {p: obj for p, _, obj in entries}
    for index, (path, mode, obj) in enumerate(entries, 1):
        problem = path_problem(path, policy, mode)
        if problem or content_problems(path.encode(), policy, profile=profile):
            reason = problem or 'potentially private source filename'
            raise Blocked(f'File #{index}: {reason}; private filename suppressed.')
        if obj in seen:
            continue
        data = blob_bytes(obj)
        findings = content_problems(data, policy, obj, profile)
        if findings:
            rule, line = findings[0]
            raise Blocked(f'{path}: {rule}, line {line or "not displayed"}; matched values suppressed.')
        if stage_bytes is not None:
            stage_bytes.append(data)
        seen.add(obj)
    return lambda p: blob_bytes(objects[p])


def main(argv=None, stdin=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--staged', action='store_true')
    group.add_argument('--history', action='store_true')
    group.add_argument('--commit-message', type=Path)
    parser.add_argument('--hook', choices=['pre-commit', 'commit-msg', 'pre-push'])
    args = parser.parse_args(argv)
    tips, push_names = [], []
    if args.hook == 'pre-push':
        tips, push_names = pushed_updates(stdin or sys.stdin)
    if args.hook:
        verify_reviewed_guards([':0'] if args.hook != 'pre-push' else ['HEAD', *tips])
    policy_source = ':0:.privacy-policy.json' if (args.staged or args.commit_message) else 'HEAD:.privacy-policy.json'
    result = git('show', policy_source, check=False)
    if result.returncode:
        raise Blocked('Stage/commit the reviewed privacy policy before checking publication.')
    policy = json.loads(result.stdout)
    config_source = ':0:.gitleaks.toml' if (args.staged or args.commit_message) else 'HEAD:.gitleaks.toml'
    config = git('show', config_source, check=False)
    if config.returncode or config.stdout != (ROOT / '.gitleaks.toml').read_bytes():
        raise Blocked('Stage/commit scanner configuration changes before privacy checking.')
    allow_missing = git('config', '--bool', 'privacy.allowMissingProfile', check=False).stdout.strip() == b'true'
    profile = without_public_values(load_profile(required=bool(args.hook) and not allow_missing), policy)
    seen = set()
    if args.commit_message:
        data = args.commit_message.read_bytes()
        if content_problems(data, policy, profile=profile):
            raise Blocked('Commit message contains potentially private content; matched values suppressed.')
        run_scanner('stdin', data)
    elif args.staged:
        for kind in ['AUTHOR', 'COMMITTER']:
            ident = git('var', f'GIT_{kind}_IDENT').stdout.decode()
            match = re.match(r'(.*) <([^>]+)>', ident)
            if not match or not check_identity(*match.groups(), policy):
                raise Blocked('Use a reviewed public author alias and your GitHub noreply email in this repository.')
        payloads = []
        entries = snapshot(staged=True)
        read = inspect_snapshot(entries, policy, seen, profile, payloads)
        check_build_inputs(entries, read)
        local_build_overrides()
        run_scanner('stdin', b'\n'.join(payloads))
    else:
        if any(content_problems(name.encode(), policy, profile=profile) for name in push_names):
            raise Blocked('A pushed reference name contains potentially private content; name suppressed.')
        roots = set(git('rev-list', '--max-parents=0', '--all', *tips).stdout.decode().split())
        if not roots or not roots.issubset(set(policy['allowed_roots'])):
            raise Blocked('Unreviewed/older root history detected; publish from the clean upstream checkout.')
        commits = git('rev-list', '--all', *tips).stdout.decode().split()
        messages = reference_messages(policy, profile)
        for sha in commits:
            raw = git('show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce%x00%B', sha).stdout
            fields = raw.decode().split('\0', 4)
            if (len(fields) != 5 or not check_identity(fields[0], fields[1], policy)
                    or not check_identity(fields[2], fields[3], policy)):
                raise Blocked('History contains an unreviewed author identity; identity values suppressed.')
            if content_problems(fields[4].encode(), policy, profile=profile):
                raise Blocked('History contains a potentially private commit message; values suppressed.')
            messages.append(fields[4].encode())
            inspect_snapshot(snapshot(commit=sha), policy, seen, profile)
        current = snapshot(commit='HEAD')
        read = lambda p: blob_bytes(dict((p, obj) for p, _, obj in current)[p])
        check_build_inputs(current, read)
        local_build_overrides()
        run_scanner('git', log_options=' '.join(['--all', *tips]))
        run_scanner('stdin', b'\n'.join(messages))
    print('Privacy checks passed. Review new prose, examples, and policy changes before publication.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Only our fixed diagnostics may be displayed, never raw OS/parser errors.
        print('Privacy check blocked: ' + (str(exc) if isinstance(exc, Blocked)
              else 'inspection could not complete; private details suppressed.'), file=sys.stderr)
        sys.exit(1)
