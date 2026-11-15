"""Explicit browser-origin boundary; service-to-service requests remain supported."""
import os
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
DEFAULT_ORIGINS = 'http://localhost,http://127.0.0.1,http://localhost:5050,http://127.0.0.1:5050'


def normalize_origin(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if len(value) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path or parsed.query or parsed.fragment or '%' in parsed.netloc:
            return None
        host = parsed.hostname.lower()
        if not all(c.isalnum() or c in '.-:' for c in host):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        if ':' in host:
            host = '[' + host + ']'
        suffix = '' if port is None or (parsed.scheme, port) in {('http', 80), ('https', 443)} else ':' + str(port)
        return parsed.scheme + '://' + host + suffix
    except ValueError:
        return None


def configured_origins(raw):
    values = [part.strip() for part in raw.split(',') if part.strip()]
    normalized = [normalize_origin(value) for value in values]
    if not values or any(value is None for value in normalized):
        raise ValueError('PDFSEARCH_ALLOWED_ORIGINS must contain explicit HTTP(S) origins')
    return frozenset(normalized)


def request_allowed(method, origin, fetch_site, referer, origins):
    if origin is not None:
        return normalize_origin(origin) in origins
    if method.upper() in SAFE_METHODS:
        # OAuth GET redirects and normal navigation carry no Origin header.
        return True
    site = (fetch_site or '').lower()
    if site == 'cross-site':
        return False
    if site in {'same-site', 'same-origin'}:
        try:
            parsed = urlsplit(referer or '')
            ref_origin = normalize_origin(parsed.scheme + '://' + parsed.netloc)
        except ValueError:
            return False
        return ref_origin in origins
    # Browserless mail/import/maintenance clients do not send Fetch Metadata.
    # Their existing authentication/authorization is still applied by the app.
    return site in {'', 'none'}


def install_browser_security(app):
    from flask import jsonify, request
    from flask_cors import CORS

    origins = configured_origins(os.environ.get('PDFSEARCH_ALLOWED_ORIGINS', DEFAULT_ORIGINS))
    app.config['PDFSEARCH_ALLOWED_ORIGINS'] = sorted(origins)
    CORS(app, resources={r'/*': {'origins': sorted(origins)}},
         supports_credentials=False, always_send=False, vary_header=True,
         allow_headers=['Content-Type', 'Authorization', 'Cache-Control'],
         methods=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
         max_age=600)

    @app.before_request
    def enforce_browser_origin():
        if not request_allowed(request.method, request.headers.get('Origin'),
                               request.headers.get('Sec-Fetch-Site'),
                               request.headers.get('Referer'), origins):
            response = jsonify({'error': 'Browser origin is not allowed'})
            response.status_code = 403
            response.headers['Cache-Control'] = 'no-store'
            return response

    @app.after_request
    def vary_browser_origin(response):
        response.vary.add('Origin')
        return response
