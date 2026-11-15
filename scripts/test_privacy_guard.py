"""Exercise real staging/push checks in disposable clones, never production."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import privacy_profile

import privacy_check as guard


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='publication-tests-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        subprocess.run(['git', 'clone', '--no-local', '--quiet', str(guard.ROOT), str(self.repo)],
                       capture_output=True, check=True)
        policy = json.loads(guard.POLICY.read_text())
        for path in guard.ROOT.rglob('*'):
            rel = path.relative_to(guard.ROOT).as_posix()
            if not path.is_file() or guard.path_problem(rel, policy):
                continue
            dest = self.repo / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        tool = self.repo / '.git/privacy-tools/gitleaks'
        tool.parent.mkdir(parents=True)
        shutil.copy2(guard.scanner_path(), tool)
        self.git('config', 'user.name', 'PDFSearch contributors')
        self.git('config', 'user.email', 'contributors@users.noreply.github.com')
        self.git('config', 'core.hooksPath', '.githooks')
        self.git('add', '--all')

    def git(self, *args, input=None):
        return subprocess.run(['git', '-C', str(self.repo), *args], input=input,
                              capture_output=True, env=dict(os.environ,
                              GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1'))

    def check(self, *args):
        return subprocess.run([sys.executable, str(self.repo / 'scripts/privacy_check.py'), *args],
                              cwd=self.repo, capture_output=True)

    def write(self, path, content):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def test_ignored_local_data_stays_local_under_add_all(self):
        paths = ['app/.env', 'app/compose.override.yaml', 'docker-compose.override.yaml',
                 'app/docker-compose.override.yaml', 'app/backend/roles.json.bak',
                 'app/data/local.pdf', 'app/private-backup.zip', 'app/backend/local_settings.py',
                 'app/backend/roles.json.copy', 'app/frontend/settings.js.copy.js']
        for p in paths:
            self.write(p, 'synthetic local data')
        self.git('add', '--all')
        tracked = self.git('ls-files', '-z').stdout.split(b'\0')
        for p in paths:
            self.assertNotIn(p.encode(), tracked)
        result = self.check('--staged')
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_force_added_access_rules_are_blocked_without_printing_filename(self):
        path = 'app/data/synthetic-private-name.json'
        self.write(path, '{}')
        self.git('add', '-f', path)
        result = self.check('--staged')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'synthetic-private-name', result.stderr)
        self.assertIn(b'private', result.stderr)

    def test_secret_in_normal_source_is_blocked_and_redacted(self):
        token = 'gh' + 'p_' + 'AbCd0123456789efGHijkLMNopQRSTuvWXYZab'
        path = 'app/frontend/config.js'
        with (self.repo / path).open('a') as f:
            f.write('\n// ' + token + '\n')
        self.git('add', path)
        result = self.check('--staged')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(token.encode(), result.stdout + result.stderr)

    def test_new_feature_module_requires_explicit_build_inputs(self):
        path = 'app/backend/new_feature.py'
        self.write(path, 'def feature():\n    return 42\n')
        self.git('add', path)
        result = self.check('--staged')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'allowlists', result.stderr)
        for config, rule in [('app/backend/.dockerignore', '!new_feature.py'),
                             ('app/.dockerignore', '!backend/new_feature.py')]:
            with (self.repo / config).open('a') as f:
                f.write(rule + '\n')
            self.git('add', config)
        result = self.check('--staged')
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_symlink_cannot_smuggle_local_configuration(self):
        p = self.repo / 'app/backend/linked.py'
        p.symlink_to('/path/to/private/configuration')
        self.git('add', str(p))
        result = self.check('--staged')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'symlink', result.stderr)

    def test_personal_commit_identity_is_blocked_without_echoing_it(self):
        email = 'private-owner' + '@' + 'mailhost' + '.com'
        self.git('config', 'user.email', email)
        result = self.check('--staged')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(email.encode(), result.stderr)
        self.assertIn(b'noreply', result.stderr)

    def test_imported_orphan_history_is_blocked(self):
        committed = self.git('-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-qm', 'Add reviewed publication controls')
        self.assertEqual(committed.returncode, 0)
        tree = self.git('mktree', input=b'').stdout.strip()
        commit = self.git('commit-tree', tree.decode(), input=b'Synthetic independent root\n').stdout.strip()
        self.git('update-ref', 'refs/heads/synthetic-import', commit.decode())
        result = self.check('--history')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'root history', result.stderr)

    def test_private_annotated_tag_identity_is_blocked(self):
        result = self.git('commit', '--allow-empty', '-qm', 'Add reviewed publication controls')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        email = 'private-owner' + '@' + 'mailhost' + '.com'
        result = self.git('-c', 'user.email=' + email, 'tag', '-a', 'v0.0.0-test', '-m', 'Synthetic release')
        self.assertEqual(result.returncode, 0)
        result = self.check('--history')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'tag identity', result.stderr)
        self.assertNotIn(email.encode(), result.stdout + result.stderr)

    def test_known_private_value_fingerprints_block_unusual_secrets(self):
        value = 'synthetic-value' + '-with-an-unusual-format'
        result = subprocess.run([sys.executable, str(self.repo / 'scripts/privacy_profile.py')],
                                input=json.dumps([value]).encode(), capture_output=True)
        self.assertEqual(result.returncode, 0)
        profile = self.repo / '.git/privacy/private-fingerprints.json'
        self.assertNotIn(value, profile.read_text())
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        with (self.repo / 'app/frontend/config.js').open('a') as f:
            f.write('\n// "' + value + '"\n')
        self.git('add', 'app/frontend/config.js')
        result = self.check('--staged')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'known-private-local-value', result.stderr)
        self.assertNotIn(value.encode(), result.stderr)

    def test_real_commit_hook_blocks_force_added_private_file(self):
        path = 'app/data/synthetic-private-name.json'
        self.write(path, '{}')
        self.git('add', '-f', path)
        result = self.git('commit', '--allow-empty', '-qm', 'Synthetic attempted private addition')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'synthetic-private-name', result.stdout + result.stderr)

    def test_clean_commit_and_real_push_hook_allow_feature_development(self):
        result = self.git('commit', '--allow-empty', '-qm', 'Add reviewed publication controls')
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        result = subprocess.run(['sh', str(self.repo / '.githooks/pre-push')],
                                cwd=self.repo, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_real_commit_message_hook_blocks_private_prose(self):
        message = 'Contact ' + 'private-owner' + '@' + 'mailhost' + '.com'
        result = self.git('commit', '--allow-empty', '-qm', message)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(message.encode(), result.stdout + result.stderr)
        self.assertIn(b'Commit message', result.stderr)

    def test_known_document_name_in_unquoted_prose_is_blocked(self):
        value = 'Synthetic sensitive' + ' document' + '.pdf'
        result = subprocess.run([sys.executable, str(self.repo / 'scripts/privacy_profile.py')],
                                input=json.dumps([value]).encode(), capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.write('docs/feature.md', 'An example refers to ' + value + '.\n')
        self.git('add', 'docs/feature.md')
        result = self.check('--staged')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'known-private-local-value', result.stderr)
        self.assertNotIn(value.encode(), result.stdout + result.stderr)

    def test_profile_refresh_does_not_approve_a_newly_committed_private_value(self):
        value = 'synthetic-new-value' + '-with-no-provider-pattern'
        self.write('app/.env', 'MAIL_TOKEN=' + value + '\n')
        with (self.repo / 'app/frontend/config.js').open('a') as f:
            f.write('\n// "' + value + '"\n')
        self.git('add', 'app/frontend/config.js')
        committed = self.git('-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-qm', 'Synthetic accidental addition')
        self.assertEqual(committed.returncode, 0)
        with mock.patch.object(guard, 'ROOT', self.repo), mock.patch('shutil.which', return_value=None):
            privacy_profile.refresh_local()
        result = self.check('--history')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'known-private-local-value', result.stderr)
        self.assertNotIn(value.encode(), result.stdout + result.stderr)

    def test_commit_message_hook_blocks_private_content(self):
        message = 'contact ' + 'private-owner' + '@' + 'mailhost' + '.com'
        p = self.repo / '.git/SYNTHETIC_MSG'
        p.write_text(message)
        result = self.check('--commit-message', str(p))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(message.encode(), result.stderr)


if __name__ == '__main__':
    unittest.main()
