"""Bounded HTTP fetching and HLS probing (mirrors the TV's CatalogNetwork). TLS verification stays on."""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .core import stream_allowed

USER_AGENT = "LiveTV-manifest/1.0"


class ProbeError(Exception):
    pass


def _check_public(url: str, http_hosts: frozenset[str]) -> None:
    if not stream_allowed(url, http_hosts):
        raise ProbeError("Unsupported stream address")
    u = urllib.parse.urlsplit(url)
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as e:
        raise ProbeError("DNS lookup failed") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ProbeError("Non-public stream address")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def fetch(url: str, limit: int, *, sample: bool = False, timeout: float = 7,
          http_hosts: frozenset[str] = frozenset(), check: bool = True) -> tuple[bytes, str]:
    for _ in range(5):
        if check:
            _check_public(url, http_hosts)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            resp = _opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                location = e.headers.get("Location")
                if not location:
                    raise ProbeError("Invalid redirect")
                nxt = urllib.parse.urljoin(url, location)
                if urllib.parse.urlsplit(nxt).scheme != urllib.parse.urlsplit(url).scheme:
                    raise ProbeError("Protocol redirect rejected")
                url = nxt
                continue
            raise ProbeError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", e)
            raise ProbeError(str(reason)[:140] or "Connection failed") from e
        with resp:
            if resp.status != 200:
                raise ProbeError(f"HTTP {resp.status}")
            try:
                data = resp.read(limit)
                if not sample and len(data) == limit and resp.read(1):
                    raise ProbeError("Response too large")
            except (http.client.HTTPException, OSError, ValueError) as e:
                # A server that drops the connection mid-body is a failed stream, not a failed run.
                raise ProbeError(f"Incomplete response ({type(e).__name__})") from e
            return data, url
    raise ProbeError("Too many redirects")


def probe(url: str, *, http_hosts: frozenset[str], timeout: float = 7, depth: int = 0) -> None:
    """Raise ProbeError unless the HLS playlist and one media sample are reachable and unencrypted."""
    if depth > 3:
        raise ProbeError("Nested playlist limit")
    data, final = fetch(url, 256000, timeout=timeout, http_hosts=http_hosts)
    text = data.decode("utf-8", "replace").strip()
    if not text.startswith("#EXTM3U"):
        raise ProbeError("Not an HLS playlist")
    if re.search(r"#EXT-X-KEY:.*METHOD=(?!NONE)[A-Z0-9-]+", text):
        raise ProbeError("Encrypted stream; not checked")
    lines = re.split(r"\r?\n", text)
    last = None
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            probe(urllib.parse.urljoin(final, lines[i + 1].strip()), http_hosts=http_hosts, timeout=timeout, depth=depth + 1)
            return
        if line and not line.startswith("#"):
            last = line
    if last is None:
        raise ProbeError("No media segments")
    sample, _ = fetch(urllib.parse.urljoin(final, last), 4096, sample=True, timeout=timeout, http_hosts=http_hosts)
    box = sample[4:8] if len(sample) >= 8 else b""
    if len(sample) < 188 or not (sample[0] == 0x47 or box in (b"ftyp", b"styp", b"moof", b"sidx")):
        raise ProbeError("Invalid media sample")
