"""Meaningful negative browser-boundary tests without live services or writes."""
import unittest
from browser_security import configured_origins, normalize_origin, request_allowed

TRUSTED = 'https://pdf.example.invalid:55559'
ORIGINS = configured_origins(TRUSTED + ',https://pdf.example.invalid:5050,http://localhost')


class BrowserBoundaryTests(unittest.TestCase):
    def test_valid_origins(self):
        for value in ORIGINS:
            for method in ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']:
                with self.subTest(origin=value, method=method):
                    self.assertTrue(request_allowed(method, value, 'same-site', None, ORIGINS))

    def test_host_port_and_scheme_are_part_of_origin(self):
        for value in ['http://pdf.example.invalid:55559',
                      'https://pdf.example.invalid:55558',
                      TRUSTED + '.evil.invalid',
                      'https://pdf.example.invalid.evil.invalid:55559',
                      'https://audit-origin.invalid', 'null', '*', '']:
            with self.subTest(origin=value):
                self.assertFalse(request_allowed('GET', value, None, None, ORIGINS))

    def test_malformed_origins_never_match(self):
        for value in [TRUSTED + '/', TRUSTED + '/path', TRUSTED + '?x=1',
                      TRUSTED + '#x', ' https://localhost', 'https://localhost ',
                      'https://user@localhost', 'https://user:password@localhost',
                      'https://local%68ost', 'https://localhost:99999',
                      'https://localhost:bad', 'https://[::1',
                      'https://localhost\n', TRUSTED + ', https://evil.invalid']:
            with self.subTest(origin=value):
                self.assertIsNone(normalize_origin(value))

    def test_default_ports_and_case_normalize(self):
        self.assertEqual(normalize_origin('https://LOCALHOST:443'), 'https://localhost')
        self.assertEqual(normalize_origin('http://localhost:80'), 'http://localhost')

    def test_wildcard_or_invalid_config_fails_closed(self):
        for raw in ['', '*', 'null', 'https://localhost/path']:
            with self.subTest(config=raw):
                with self.assertRaises(ValueError):
                    configured_origins(raw)

    def test_service_calls_without_browser_headers_continue(self):
        for method in ['GET', 'POST', 'PUT', 'DELETE']:
            self.assertTrue(request_allowed(method, None, None, None, ORIGINS))

    def test_simple_cross_site_writes_are_rejected_without_origin(self):
        for method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            self.assertFalse(request_allowed(method, None, 'cross-site', None, ORIGINS))
            self.assertFalse(request_allowed(method, None, 'same-site', 'https://evil.invalid/', ORIGINS))
            self.assertFalse(request_allowed(method, None, 'same-origin', None, ORIGINS))

    def test_approved_browser_origin_allows_legitimate_cross_site_frontend(self):
        self.assertTrue(request_allowed('POST', TRUSTED, 'cross-site', None, ORIGINS))

    def test_oauth_callback_navigation_still_works(self):
        self.assertTrue(request_allowed('GET', None, 'cross-site', 'https://accounts.google.com/', ORIGINS))

    def test_origin_takes_precedence_over_forged_fetch_metadata(self):
        self.assertFalse(request_allowed('POST', 'https://evil.invalid', 'same-origin', TRUSTED + '/', ORIGINS))


if __name__ == '__main__':
    unittest.main()
