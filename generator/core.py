"""Pure channel-catalogue logic: parsing, policy, categorisation, ordering and numbering.

No network access here, so everything is unit-testable. Behaviour mirrors the TV-side logic
that shipped in Live TV 1.1.0 (versionCode 18) so migrating keeps identities and numbers.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field

MANIFEST_SCHEMA = 1


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    group: str
    url: str
    logo: str
    headers: bool


def base_id(channel_id: str) -> str:
    return channel_id.split("@", 1)[0]


_ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')


def parse_m3u(text: str) -> list[Item]:
    """Parse iptv-org extended M3U. Entries needing extra request headers are flagged, never imported."""
    if text.startswith("﻿"):
        text = text[1:]
    if len(text) > 4_000_000 or not text.strip().startswith("#EXTM3U"):
        raise ValueError("Invalid playlist")
    result: list[Item] = []
    attrs = None
    name = ""
    headers = False
    for raw in re.split(r"\r?\n", text):
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            if attrs is not None:
                raise ValueError("Missing stream URL")
            attrs = {k.lower(): v for k, v in _ATTRIBUTE.findall(line)}
            quoted = False
            comma = -1
            for i, ch in enumerate(line):
                if ch == '"':
                    quoted = not quoted
                elif ch == "," and not quoted:
                    comma = i
                    break
            if comma < 0:
                raise ValueError("Missing channel name")
            name = line[comma + 1:].strip()
            headers = "http-referrer" in attrs or "http-user-agent" in attrs
        elif line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTHTTP:")):
            headers = True
        elif line and not line.startswith("#"):
            if attrs is None or not name:
                raise ValueError("Missing channel metadata")
            cid = attrs.get("tvg-id", name).strip() or name
            if len(cid) > 256 or len(name) > 256 or len(line) > 4096:
                raise ValueError("Oversized entry")
            result.append(Item(cid, name, attrs.get("group-title", "Other"), line, attrs.get("tvg-logo", ""), headers))
            if len(result) > 10000:
                raise ValueError("Too many entries")
            attrs = None
    if attrs is not None or not result:
        raise ValueError("Incomplete playlist")
    return result


# ------------------------------------------------------------------ URL policy (mirrors ChannelPolicy)

_SECRET_QUERY = re.compile(r"(?i)(?:^|&)(?:token|auth_key|hdnts|hdnea|password|username|mac|play_token)=")


def stream_allowed(url: str, http_hosts: frozenset[str] = frozenset()) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.fragment:
        return False
    host = u.hostname.lower()
    if ("." not in host or ":" in host or host.endswith((".local", ".lan"))
            or re.match(r"(?:0|10|127)\.", host) or host.startswith(("192.168.", "169.254."))
            or re.match(r"172\.(?:1[6-9]|2[0-9]|3[01])\.", host)):
        return False
    if u.scheme == "http" and host not in http_hosts:
        return False
    return not (u.query and _SECRET_QUERY.search(u.query))


def logo_allowed(url: str) -> bool:
    return bool(url) and len(url) <= 4096 and url.startswith("https://") and stream_allowed(url)


# ------------------------------------------------------------------ content policy (mirrors ContentFilter)

_ADULT = re.compile(r"(?i)(?:^|[^a-z0-9])(?:nsfw|xxx|adult|porn|pornography|erotic|18\+)(?:$|[^a-z0-9])")


@dataclass(frozen=True)
class Classification:
    nsfw: bool
    religious: bool


def exclusion_reason(cid: str, name: str, group: str, info: Classification | None, policy: dict) -> str:
    base = base_id(cid)
    if base in policy.get("excluded_ids", []):
        return "Excluded: owner channel selection"
    if (info is not None and info.nsfw) or _ADULT.search(name + " " + group):
        return "Excluded: adult/NSFW"
    if info is None and policy.get("require_classification", True):
        return "Excluded: source classification not available"
    religious = (info is not None and info.religious) or base in policy.get("religious_override_ids", []) \
        or re.search(r"religious|devotional", group.lower()) is not None
    if religious and base not in policy.get("allowed_devotional_ids", []):
        return "Excluded: devotional channel outside Hindu selection"
    return ""


def parse_classifications(data: list, needed: set[str] | None = None) -> dict[str, Classification]:
    result: dict[str, Classification] = {}
    if not isinstance(data, list) or len(data) > 200000:
        raise ValueError("Invalid classification metadata")
    for row in data:
        cid = row.get("id")
        if not cid or (needed is not None and cid not in needed):
            continue
        nsfw = row.get("is_nsfw")
        if not isinstance(nsfw, bool) or cid in result:
            raise ValueError("Invalid classification metadata")
        cats = [str(c).lower() for c in row.get("categories") or []]
        result[cid] = Classification(nsfw, "religious" in cats)
    if not result:
        raise ValueError("Empty classification metadata")
    return result


# ------------------------------------------------------------------ categories & ordering

def _tags(group: str) -> set[str]:
    return {t.strip() for t in re.split(r"[;,]", group.lower())}


class Layout:
    """Languages, categories and classification rules from the config."""

    def __init__(self, config: dict):
        self.languages = config["languages"]
        self.categories = config["categories"]
        self.lang_ids = [l["id"] for l in self.languages]
        self.by_id = {c["id"]: c for c in self.categories}
        if len(self.by_id) != len(self.categories):
            raise ValueError("Duplicate category id")
        for c in self.categories:
            if c["language"] not in self.lang_ids:
                raise ValueError(f"Category {c['id']} has unknown language")
        self.index = {c["id"]: i for i, c in enumerate(self.categories)}
        self.classify_order = config.get("classify_order", {})
        self.listed: dict[str, dict[str, str]] = {}
        for lang in self.languages:
            if lang.get("selection") == "listed":
                ids: dict[str, str] = {}
                for c in self.categories:
                    if c["language"] == lang["id"]:
                        for i in c.get("ids", []):
                            if i in ids:
                                raise ValueError(f"{i} listed twice")
                            ids[i] = c["id"]
                self.listed[lang["id"]] = ids

    def categorise(self, language: str, cid: str, group: str) -> str:
        lang = next(l for l in self.languages if l["id"] == language)
        base = base_id(cid)
        if lang.get("selection") == "listed":
            return self.listed[language][base]
        tags = _tags(group)
        for cat_id in self.classify_order.get(language, []):
            c = self.by_id[cat_id]
            if base in c.get("ids", []) or tags & set(c.get("tags", [])):
                return cat_id
        return lang["fallback_category"]

    def sort_key(self, language: str, category: str, cid: str, name: str):
        pinned = self.by_id[category].get("pinned", [])
        base = base_id(cid)
        priority = pinned.index(base) if base in pinned else len(pinned)
        return (self.lang_ids.index(language), self.index[category], priority, _java_ci(name), cid)


def _java_ci(value: str) -> str:
    # Equivalent of Java's String.CASE_INSENSITIVE_ORDER for the names in use.
    return "".join(ch.upper().lower() for ch in value)


def assign_service_ids(ids, history: dict[str, int], first: int) -> dict[str, int]:
    """Stable internal service IDs: existing values never change; new IDs continue after the max."""
    result = dict(history)
    nxt = max([first - 1, *result.values()]) + 1
    for cid in sorted(ids):
        if cid not in result:
            result[cid] = nxt
            nxt += 1
    if len(set(result.values())) != len(result):
        raise ValueError("Duplicate service id in history")
    return result


def content_hash(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k not in ("version", "generated_at")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class Resolved:
    id: str
    name: str
    language: str
    category: str
    logo: str
    url: str
    available: bool
    reason: str
    excluded: bool = False


@dataclass
class BuildResult:
    manifest: dict
    rows: list[Resolved] = field(default_factory=list)


def build_manifest(config: dict, layout: Layout, rows: list[Resolved], service_ids: dict[str, int],
                   version: int, generated_at: str) -> dict:
    visible = [r for r in rows if not r.excluded]
    ordered = sorted(visible, key=lambda r: (not r.available,) + layout.sort_key(r.language, r.category, r.id, r.name))
    # Display numbers: contiguous across available channels, languages in tab order.
    available = [r for r in ordered if r.available]
    available.sort(key=lambda r: layout.sort_key(r.language, r.category, r.id, r.name))
    channels = []
    for n, r in enumerate(available, start=1):
        channels.append({
            "id": r.id, "service_id": service_ids[r.id], "number": str(n), "name": r.name,
            "language": r.language, "category": r.category, "logo": r.logo, "url": r.url,
        })
    # Reasons stay in the report: they fluctuate between runs and would bump the version needlessly.
    unavailable = [{"id": r.id, "name": r.name, "language": r.language, "category": r.category}
                   for r in ordered if not r.available]
    if len(channels) + len(unavailable) > config["publish"]["max_channels"]:
        raise ValueError("Too many channels")
    if not channels:
        raise ValueError("No available channels; refusing to publish an empty manifest")
    return {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "generated_at": generated_at,
        "min_app_version": config["publish"]["min_app_version"],
        "refresh_minutes": config["publish"]["refresh_minutes"],
        "languages": [{"id": l["id"], "name": l["name"], "audio": l.get("audio", [l["id"]])} for l in layout.languages],
        "categories": [{"id": c["id"], "language": c["language"], "name": c["name"]} for c in layout.categories],
        "channels": channels,
        "unavailable": unavailable,
    }
