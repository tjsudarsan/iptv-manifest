"""Generate docs/v1/manifest.json from config/channels.toml, the live sources and state/state.json.

Usage: python -m generator.generate [--offline-sources DIR] [--no-probe] [--dry-run]

Stream availability rules (same as the TV app used):
  * an unchanged, previously available URL is kept without re-probing;
  * otherwise headerless source options are probed in order and the first that passes is used;
  * if every option fails, a previously available URL is retained;
  * `overrides.<id>.url` forces a stream and is always treated as available.
The manifest version only increases when published content changes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import core, net

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "channels.toml"
STATE = ROOT / "state" / "state.json"
MANIFEST = ROOT / "docs" / "v1" / "manifest.json"
REPORT = ROOT / "reports" / "latest.md"


def load_config(path: Path = CONFIG) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_state(path: Path = STATE) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 0, "content_hash": "", "service_ids": {}, "streams": {}, "source_counts": {}}


class Sources:
    def __init__(self, config: dict, offline: Path | None):
        self.config = config
        self.offline = offline
        net_cfg = config["network"]
        self.timeout = net_cfg.get("timeout_seconds", 7)

    def _get(self, url: str, limit: int, name: str) -> bytes:
        if self.offline:
            return (self.offline / name).read_bytes()
        data, _ = net.fetch(url, limit, timeout=30)
        return data

    def playlist(self, lang: dict) -> list[core.Item]:
        return core.parse_m3u(self._get(lang["source"], 4_000_000, f"{lang['id']}.m3u").decode("utf-8"))

    def classifications(self, needed: set[str]) -> dict[str, core.Classification]:
        raw = self._get(self.config["classification"]["source"], 64_000_000, "channels.json")
        return core.parse_classifications(json.loads(raw), needed)


def select(config: dict, layout: core.Layout, playlists: dict[str, list[core.Item]],
           previous_counts: dict[str, int]) -> dict[str, tuple[str, list[core.Item]]]:
    """Group source items by channel ID per language, with per-language truncation guards."""
    groups: dict[str, tuple[str, list[core.Item]]] = {}
    for lang in layout.languages:
        items = playlists[lang["id"]]
        if lang.get("selection") == "listed":
            wanted = layout.listed[lang["id"]]
            picked, seen = [], set()
            for it in items:
                b = core.base_id(it.id)
                if b in wanted and (b, it.url, it.headers) not in seen:
                    seen.add((b, it.url, it.headers))
                    picked.append(core.Item(b, it.name, it.group, it.url, it.logo, it.headers))
            items = picked
        elif not lang.get("keep_feed_suffix", True):
            items = [core.Item(core.base_id(i.id), i.name, i.group, i.url, i.logo, i.headers) for i in items]
        ids = {i.id for i in items}
        if len(ids) < max(1, previous_counts.get(lang["id"], 0) // 2):
            raise ValueError(f"Incomplete {lang['id']} source ({len(ids)} channels)")
        for cid in ids:
            if cid in groups:
                raise ValueError(f"Conflicting language identity {cid}")
        for it in items:
            groups.setdefault(it.id, (lang["id"], []))[1].append(it)
    for manual in config.get("manual", []):
        if manual["id"] in groups:
            raise ValueError(f"Manual channel {manual['id']} duplicates a source channel")
        groups[manual["id"]] = (manual["language"], [core.Item(manual["id"], manual["name"], "", manual["url"],
                                                                manual.get("logo", ""), False)])
    if len(groups) > 2000:
        raise ValueError("Too many selected channels")
    return groups


def resolve(cid: str, language: str, options: list[core.Item], layout: core.Layout, config: dict,
            info: core.Classification | None, previous: dict | None, probe, manual: bool) -> core.Resolved:
    override = config.get("overrides", {}).get(cid, {})
    first = options[0]
    category = override.get("category") or (next(m for m in config.get("manual", []) if m["id"] == cid)["category"]
                                            if manual else layout.categorise(language, cid, first.group))
    if category not in layout.by_id or layout.by_id[category]["language"] != language:
        raise ValueError(f"{cid}: invalid category {category}")

    def row(item: core.Item, url: str, available: bool, reason: str, excluded: bool = False) -> core.Resolved:
        # Name and artwork follow the chosen source option, as on the TV; overrides win.
        logo = override.get("logo") or (item.logo if core.logo_allowed(item.logo) else
                                        next((o.logo for o in options if core.logo_allowed(o.logo)), ""))
        return core.Resolved(cid, override.get("name", item.name), language, category,
                             logo if core.logo_allowed(logo) else "", url, available, reason, excluded)

    if override.get("disabled"):
        return row(first, "", False, "Excluded: disabled in config", excluded=True)
    if not manual:
        for o in options:
            reason = core.exclusion_reason(cid, o.name, o.group, info, config["policy"])
            if reason:
                return row(o, "", False, reason, excluded=True)
    http_hosts = frozenset(config["network"].get("http_allowed_hosts", []))
    forced = override.get("url") or (options[0].url if manual else "")
    if forced:
        if not core.stream_allowed(forced, http_hosts):
            raise ValueError(f"{cid}: configured URL is not allowed")
        return row(first, forced, True, "Configured stream")
    if override.get("unavailable"):
        return row(first, "", False, str(override["unavailable"]))
    old_ok = previous is not None and previous.get("available") and previous.get("url")
    if old_ok:
        for o in options:
            if not o.headers and o.url == previous["url"]:
                return row(o, o.url, True, "Previously checked stream; source current")
    reason = "No supported stream"
    for o in options:
        if o.headers:
            reason = "Requires additional request headers"
            continue
        if not core.stream_allowed(o.url, http_hosts):
            reason = "Unsupported stream address"
            continue
        try:
            probe(o.url)
            return row(o, o.url, True, "Playlist and media sample checked")
        except Exception as e:  # any probe failure (even an unexpected one) only fails this option
            reason = str(e) or "Stream check failed"
    if old_ok:
        return row(first, previous["url"], True, "Source change failed checks; retained previous stream")
    return row(first, "", False, reason[:140])


def run(config: dict, state: dict, sources: Sources, probe_enabled: bool = True, now: str | None = None):
    layout = core.Layout(config)
    playlists = {l["id"]: sources.playlist(l) for l in layout.languages}
    groups = select(config, layout, playlists, state.get("source_counts", {}))
    manual_ids = {m["id"] for m in config.get("manual", [])}
    metadata = sources.classifications({core.base_id(c) for c in groups if c not in manual_ids})
    http_hosts = frozenset(config["network"].get("http_allowed_hosts", []))
    timeout = config["network"].get("timeout_seconds", 7)

    def probe(url: str) -> None:
        if not probe_enabled:
            raise net.ProbeError("Probing disabled")
        net.probe(url, http_hosts=http_hosts, timeout=timeout)

    streams = state.get("streams", {})
    with ThreadPoolExecutor(max_workers=config["network"].get("probe_workers", 8)) as pool:
        futures = {cid: pool.submit(resolve, cid, lang, opts, layout, config, metadata.get(core.base_id(cid)),
                                    streams.get(cid), probe, cid in manual_ids)
                   for cid, (lang, opts) in sorted(groups.items())}
        rows = [f.result() for f in futures.values()]

    service_ids = core.assign_service_ids([r.id for r in rows], state.get("service_ids", {}),
                                          config["publish"]["first_service_id"])
    now = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    draft = core.build_manifest(config, layout, rows, service_ids, state.get("version", 0), now)
    digest = core.content_hash(draft)
    changed = digest != state.get("content_hash")
    version = state.get("version", 0) + (1 if changed else 0)
    manifest = core.build_manifest(config, layout, rows, service_ids, version,
                                   now if changed else state.get("generated_at", now))
    new_state = {
        "version": version,
        "content_hash": digest,
        "generated_at": manifest["generated_at"],
        "service_ids": dict(sorted(service_ids.items())),
        "streams": {r.id: {"url": r.url, "available": r.available} for r in sorted(rows, key=lambda r: r.id)
                    if not r.excluded},
        "source_counts": {lang: len({c for c, (l, _) in groups.items() if l == lang and c not in manual_ids})
                          for lang in layout.lang_ids},
    }
    return manifest, new_state, rows, changed


def report(manifest: dict, rows: list[core.Resolved]) -> str:
    cats = {c["id"]: c for c in manifest["categories"]}
    lines = [f"# Live TV manifest report", "",
             f"Version {manifest['version']} · generated {manifest['generated_at']} · "
             f"{len(manifest['channels'])} channels, {len(manifest['unavailable'])} unavailable, "
             f"{sum(r.excluded for r in rows)} excluded", ""]
    for lang in manifest["languages"]:
        lines += [f"## {lang['name']}", ""]
        for cat in [c for c in manifest["categories"] if c["language"] == lang["id"]]:
            chans = [c for c in manifest["channels"] if c["category"] == cat["id"]]
            if chans:
                lines.append(f"**{cat['name']}** {chans[0]['number']}–{chans[-1]['number']}: " +
                             ", ".join(f"{c['number']} {c['name']}" for c in chans))
                lines.append("")
    reasons = {r.id: r.reason for r in rows}
    lines += ["## Unavailable", ""] + [f"- {u['name']} ({u['id']}, {cats[u['category']]['name']}): {reasons[u['id']]}"
                                       for u in manifest["unavailable"]]
    lines += ["", "## Excluded", ""] + [f"- {r.name} ({r.id}): {r.reason}" for r in sorted(rows, key=lambda r: r.id)
                                        if r.excluded]
    return "\n".join(lines) + "\n"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline-sources", type=Path, help="directory with ta.m3u, en.m3u, channels.json")
    ap.add_argument("--no-probe", action="store_true", help="never probe; changed URLs become unavailable")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    config = load_config()
    state = load_state()
    manifest, new_state, rows, changed = run(config, state, Sources(config, args.offline_sources), not args.no_probe)
    print(f"version {manifest['version']} ({'changed' if changed else 'unchanged'}): "
          f"{len(manifest['channels'])} channels, {len(manifest['unavailable'])} unavailable, "
          f"{sum(r.excluded for r in rows)} excluded")
    if not args.dry_run:
        if changed or not MANIFEST.exists():
            write_json(MANIFEST, manifest)
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(report(manifest, rows), encoding="utf-8")
        if new_state != state:
            write_json(STATE, new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
