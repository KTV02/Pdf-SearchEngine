"""Resolve browser identity from the immediate peer and deployment mode."""

from email.header import decode_header
from functools import lru_cache
import ipaddress
import logging
import os
import re
import subprocess
import time
from urllib.parse import urlsplit


RUNNING_IN_CONTAINER = os.path.isfile("/.dockerenv") or os.path.isfile("/run/.containerenv")
ASCII_LOWERCASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
LOGIN_HEADER = "Tailscale-User-Login"
LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.ASCII | re.IGNORECASE)
LOGIN_ATOM = r"[\w!#$%&'*+/=?^`{|}~-]+"
LOGIN_LABEL = r"[^\W_](?:(?:[^\W_]|-)*[^\W_])?"
LOGIN_NAME = re.compile(rf"{LOGIN_ATOM}(?:\.{LOGIN_ATOM})*@{LOGIN_LABEL}(?:\.{LOGIN_LABEL})*")
Q_WORD = r"=\?utf-8\?q\?(?:[^?=\s,]|=[0-9a-f]{2})+\?="
ENCODED_LOGIN = re.compile(rf"{Q_WORD}(?: +{Q_WORD})*", re.ASCII | re.IGNORECASE)


def normalize_login(value):
    if not isinstance(value, str) or len(value) > 1024:
        return ""
    login = value.strip(" \t")
    if login.startswith("=?"):
        # Serve sends non-ASCII logins as UTF-8 RFC 2047 Q-encoded words.
        if not login.isascii() or not ENCODED_LOGIN.fullmatch(login):
            return ""
        try:
            login = "".join(part.decode("utf-8") for part, _ in decode_header(login))
        except UnicodeDecodeError:
            return ""
    return login.translate(ASCII_LOWERCASE) if LOGIN_NAME.fullmatch(login) else ""


def parse_peer(value):
    if not value or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    address = getattr(address, "ipv4_mapped", None) or address
    return None if address.is_unspecified or address.is_multicast else address


def configure_request_identity(app):
    mode = os.environ.get("LOCAL_SINGLE_USER_MODE", "false").strip().lower()
    if mode not in {"true", "false"}:
        raise ValueError("LOCAL_SINGLE_USER_MODE must be true or false")
    app.config["LOCAL_SINGLE_USER_MODE"] = mode == "true"

    app.config["PDFSEARCH_BROWSER_HOSTS"] = frozenset(
        urlsplit(origin).hostname for origin in app.config["PDFSEARCH_ALLOWED_ORIGINS"]
    )
    proxy_ip = os.environ.get("PDFSEARCH_TRUSTED_PROXY_IP", "").strip()
    trusted_proxy = parse_peer(proxy_ip)
    if proxy_ip and trusted_proxy is None:
        raise ValueError("PDFSEARCH_TRUSTED_PROXY_IP must be one exact IP address")
    app.config["PDFSEARCH_TRUSTED_PROXY_IP"] = trusted_proxy


def get_docker_gateway_ip():
    if not RUNNING_IN_CONTAINER:
        return None
    return _gateway_for_minute(int(time.monotonic() // 60))


@lru_cache(maxsize=1)
def _gateway_for_minute(_minute):
    # A failed lookup denies trust until the next refresh. Cache misses as well
    # as successes so requests cannot repeatedly spawn the route command.
    try:
        routes = subprocess.check_output(
            ["ip", "-4", "route", "show", "default"], text=True,
            stderr=subprocess.DEVNULL, timeout=2,
        ).splitlines()
    except (OSError, subprocess.SubprocessError):
        routes = []
    if len(routes) == 1:
        route = routes[0].split()
        if len(route) >= 3 and route[:2] == ["default", "via"]:
            gateway = parse_peer(route[2])
            if gateway is not None:
                return gateway
    logging.getLogger(__name__).warning("No unambiguous Docker gateway, retrying within a minute")
    return None


def is_local_peer(peer):
    return peer is not None and (peer.is_loopback or peer == get_docker_gateway_ip())


def has_proxy_headers(headers):
    return any(
        name in {"forwarded", "via", "x-real-ip"} or name.startswith(("x-forwarded-", "tailscale-"))
        for name in (name.lower() for name in headers.keys())
    )


def browser_identity(request, config):
    # REMOTE_ADDR is the socket peer. Do not rewrite it from proxy headers.
    peer = parse_peer(request.remote_addr)
    if config["LOCAL_SINGLE_USER_MODE"]:
        if LOCAL_HOST.fullmatch(request.host) and not has_proxy_headers(request.headers) and is_local_peer(peer):
            return "local", ""
        return None

    logins = request.headers.getlist(LOGIN_HEADER)
    login = normalize_login(logins[0]) if len(logins) == 1 else ""
    if not login:
        return None
    try:
        host = urlsplit("//" + request.host)
        if (host.hostname not in config["PDFSEARCH_BROWSER_HOSTS"] or host.username is not None
                or host.path or host.query or host.fragment):
            return None
    except ValueError:
        return None
    trusted_proxy = config["PDFSEARCH_TRUSTED_PROXY_IP"]
    if (peer == trusted_proxy if trusted_proxy is not None else is_local_peer(peer)):
        return "tailscale", login
    return None
