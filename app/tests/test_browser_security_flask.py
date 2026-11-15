import os
import unittest
from unittest.mock import patch
from flask import Flask, jsonify
from browser_security import install_browser_security

TRUSTED = 'https://pdf.example.invalid:55559'


class FlaskOriginTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.app = Flask(__name__)
        with patch.dict(os.environ, {'PDFSEARCH_ALLOWED_ORIGINS': TRUSTED + ',http://localhost'}):
            install_browser_security(self.app)

        @self.app.route('/admin/action', methods=['GET', 'POST', 'PUT'])
        def action():
            self.calls.append(True)
            return jsonify({'ok': True})
        self.client = self.app.test_client()

    def test_approved_get_has_exact_origin(self):
        r = self.client.get('/admin/action', headers={'Origin': TRUSTED})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['Access-Control-Allow-Origin'], TRUSTED)
        self.assertIn('Origin', r.headers['Vary'])
        self.assertNotIn('Access-Control-Allow-Credentials', r.headers)

    def test_foreign_get_is_not_executed_or_readable(self):
        r = self.client.get('/admin/action', headers={'Origin': 'https://audit-origin.invalid'})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', r.headers)
        self.assertEqual(self.calls, [])

    def test_foreign_simple_posts_are_rejected_before_handler(self):
        for content_type in ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data', 'application/json']:
            r = self.client.post('/admin/action', data='test', content_type=content_type,
                                 headers={'Origin': 'https://audit-origin.invalid'})
            self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_preflight_is_allowed_only_for_approved_origin(self):
        for origin, expected in [(TRUSTED, 200), ('https://audit-origin.invalid', 403)]:
            r = self.client.options('/admin/action', headers={
                'Origin': origin, 'Access-Control-Request-Method': 'PUT',
                'Access-Control-Request-Headers': 'content-type'})
            self.assertEqual(r.status_code, expected)
            if expected == 200:
                self.assertEqual(r.headers['Access-Control-Allow-Origin'], TRUSTED)
                self.assertIn('PUT', r.headers['Access-Control-Allow-Methods'])
            else:
                self.assertNotIn('Access-Control-Allow-Origin', r.headers)
        self.assertEqual(self.calls, [])

    def test_legitimate_write_and_originless_service_write_work(self):
        self.assertEqual(self.client.post('/admin/action', json={}, headers={'Origin': TRUSTED}).status_code, 200)
        self.assertEqual(self.client.post('/admin/action', json={}).status_code, 200)
        self.assertEqual(len(self.calls), 2)

    def test_missing_origin_does_not_bypass_fetch_metadata(self):
        r = self.client.post('/admin/action', json={}, headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_default_responses_do_not_advertise_wildcard(self):
        r = self.client.get('/admin/action')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('Access-Control-Allow-Origin', r.headers)


if __name__ == '__main__':
    unittest.main()
