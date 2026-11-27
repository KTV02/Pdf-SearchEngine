"""Resolve browser identity from the immediate peer and deployment mode."""

from email.header import decode_header
from functools import lru_cache
import ipaddress
import os
import re
import subprocess


RUNNING_IN_DOCKER = os.path.isfile("/.dockerenv")
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
    return login.lower() if LOGIN_NAME.fullmatch(login) else ""


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

    proxy_ip = os.environ.get("PDFSEARCH_TRUSTED_PROXY_IP", "").strip()
    trusted_proxy = parse_peer(proxy_ip)
    if proxy_ip and trusted_proxy is None:
        raise ValueError("PDFSEARCH_TRUSTED_PROXY_IP must be one exact IP address")
    app.config["PDFSEARCH_TRUSTED_PROXY_IP"] = trusted_proxy


@lru_cache(maxsize=1)
def get_docker_gateway_ip():
    if not RUNNING_IN_DOCKER:
        return None
    try:
        routes = subprocess.check_output(
            ["ip", "-4", "route", "show", "default"], text=True,
            stderr=subprocess.DEVNULL, timeout=2,
        ).splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if len(routes) == 1:
        route = routes[0].split()
        if len(route) >= 3 and route[:2] == ["default", "via"]:
            return parse_peer(route[2])
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
    trusted_proxy = config["PDFSEARCH_TRUSTED_PROXY_IP"]
    if (peer == trusted_proxy if trusted_proxy is not None else is_local_peer(peer)):
        return "tailscale", login
    return None
