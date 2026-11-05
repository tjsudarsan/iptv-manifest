"""Build docs/v1/guide.json: a compact programme guide for the channels in docs/v1/manifest.json.

Sources (public, no accounts or tokens):
  * airtelxstream.in  - Airtel Xstream's channel EPG API (Indian channels)
  * i.mjh.nz          - public XMLTV files (Plex, Pluto, Samsung TV Plus, Roku, Foxtel) on GitHub
Channel -> source mapping comes from iptv-org's guides index; `[guide.overrides]` in the config can pin one.

Output (schema 1):
  {"schema": 1, "generated_at": "...", "from": <unix>, "to": <unix>,
   "channels": {"SunTV.in@HD": [[start, stop, "title", "description"], ...], ...}}
Times are Unix seconds (UTC). Programmes are sorted, non-overlapping and clipped to the window.

Usage: python -m generator.guide [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import generate, net

GUIDE = generate.ROOT / "docs" / "v1" / "guide.json"
REPORT = generate.ROOT / "reports" / "guide.md"
GUIDES_INDEX = "https://iptv-org.github.io/api/guides.json"
AIRTEL = "https://epg.airtel.tv/app/v2/content/channel/epg?channelId={id}&startTime={start}&endTime={end}"
MJH = "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/{path}.xml.gz"
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
# Preferred i.mjh.nz files, most relevant first; anything else listed in the index comes after.
MJH_ORDER = ["Plex/us", "SamsungTVPlus/us", "PlutoTV/us", "Roku/all", "Plex/gb", "Plex/au", "Plex/ca", "Foxtel/epg"]


def get(url: str, limit: int, headers: dict | None = None, timeout: float = 30) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise net.ProbeError("Response too large")
    return data


def clean(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def choose_sources(manifest: dict, index: list, overrides: dict) -> dict[str, list[tuple[str, str]]]:
    """Each manifest channel's guide sources, best first: same feed, Airtel, then preferred i.mjh.nz files."""
    by_channel: dict[str, list] = {}
    for row in index:
        if row.get("site") in ("airtelxstream.in", "i.mjh.nz") and row.get("site_id"):
            by_channel.setdefault(row["channel"], []).append(row)
    chosen = {}
    for ch in manifest["channels"]:
        cid = ch["id"]
        if cid in overrides:
            site, _, site_id = overrides[cid].partition(":")
            chosen[cid] = [(site, site_id)]
            continue
        base, _, feed = cid.partition("@")

        def rank(r):
            path = r["site_id"].split("#")[0]
            mjh = MJH_ORDER.index(path) if path in MJH_ORDER else len(MJH_ORDER)
            return (r["site"] != "airtelxstream.in", bool(feed) and r.get("feed") != feed, mjh, r["site_id"])
        rows = sorted(by_channel.get(base, []), key=rank)
        # Only well-known i.mjh.nz files are downloaded; keep at most three candidates per channel.
        rows = [r for r in rows if r["site"] == "airtelxstream.in" or r["site_id"].split("#")[0] in MJH_ORDER][:3]
        if rows:
            chosen[cid] = list(dict.fromkeys((r["site"], r["site_id"]) for r in rows))
    return chosen


def airtel(site_id: str, start: int, end: int) -> list:
    programmes = []
    for day_start in range(start, end, 86400):
        day_end = min(end, day_start + 86400)
        raw = get(AIRTEL.format(id=site_id, start=day_start * 1000, end=day_end * 1000), 4_000_000,
                  {"Referer": "https://www.airtelxstream.in/"})
        guide = json.loads(raw).get("programGuide") or {}
        for items in guide.values():
            for p in items or []:
                programmes.append((int(p["startTime"]) // 1000, int(p["endTime"]) // 1000,
                                   p.get("title") or "", p.get("desc") or ""))
    return programmes


def xmltv_time(value: str) -> int:
    return int(dt.datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z").timestamp())


def mjh(path: str, wanted: set[str]) -> dict[str, list]:
    """Parse one i.mjh.nz XMLTV file, keeping only the wanted channel IDs."""
    data = gzip.decompress(get(MJH.format(path=path), 40_000_000))
    if len(data) > 400_000_000:
        raise net.ProbeError("Guide file too large")
    result: dict[str, list] = {w: [] for w in wanted}
    # Plex IDs are "<provider>-<channel>" and the provider prefix changes; match on the channel part too.
    alias = {w.rsplit("-", 1)[-1]: w for w in wanted if "-" in w}
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        if el.tag == "programme":
            ch = el.get("channel")
            ch = ch if ch in result else alias.get((ch or "").rsplit("-", 1)[-1])
            if ch in result:
                result[ch].append((xmltv_time(el.get("start")), xmltv_time(el.get("stop")),
                                   el.findtext("title") or "", el.findtext("desc") or ""))
            el.clear()
        elif el.tag == "channel":
            el.clear()
    return result


def normalise(programmes, start: int, end: int, title_max: int, desc_max: int) -> list:
    out = []
    for s, e, title, desc in sorted(programmes):
        if e <= start or s >= end or e <= s or not str(title).strip():
            continue
        s, e = max(s, start), min(e, end)
        if out and s < out[-1][1]:
            s = out[-1][1]  # remove overlaps (sources sometimes repeat a boundary programme)
            if e <= s:
                continue
        out.append([s, e, clean(title, title_max), clean(desc, desc_max)])
    return out


def build(config: dict, manifest: dict, index: list, now: int | None = None) -> tuple[dict, dict]:
    gcfg = config.get("guide", {})
    now = now or int(time.time())
    start = now - int(gcfg.get("hours_behind", 2)) * 3600
    end = now + int(gcfg.get("hours_ahead", 36)) * 3600
    chosen = choose_sources(manifest, index, gcfg.get("overrides", {}))
    errors: dict[str, str] = {}

    fetched: dict[tuple[str, str], list] = {}
    failed: dict[tuple[str, str], str] = {}
    airtel_ids = {src for srcs in chosen.values() for src in srcs if src[0] == "airtelxstream.in"}
    with ThreadPoolExecutor(max_workers=int(gcfg.get("workers", 6))) as pool:
        futures = {src: pool.submit(airtel, src[1], start, end) for src in airtel_ids}
        for src, f in futures.items():
            try:
                fetched[src] = f.result()
            except Exception as e:  # one channel's failure never blocks the rest
                failed[src] = f"airtel: {type(e).__name__}"
    paths: dict[str, set[str]] = {}
    for srcs in chosen.values():
        for site, sid in srcs:
            if site == "i.mjh.nz":
                path, _, channel = sid.partition("#")
                paths.setdefault(path, set()).add(channel)
    for path, channels in sorted(paths.items()):
        try:
            parsed = mjh(path, channels)
            for channel in channels:
                fetched[("i.mjh.nz", f"{path}#{channel}")] = parsed.get(channel, [])
        except Exception as e:
            for channel in channels:
                failed[("i.mjh.nz", f"{path}#{channel}")] = f"i.mjh.nz {path}: {type(e).__name__}"

    channels, used = {}, {}
    for cid in [c["id"] for c in manifest["channels"]]:
        for src in chosen.get(cid, []):
            progs = normalise(fetched.get(src, []), start, end, int(gcfg.get("title_max", 80)),
                              int(gcfg.get("description_max", 160)))
            if progs:
                channels[cid], used[cid] = progs, src
                break
        else:
            if cid in chosen:
                srcs = chosen[cid]
                errors[cid] = "; ".join(failed.get(src, "no programmes in window") for src in srcs)
    guide = {"schema": 1, "generated_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "from": start, "to": end, "channels": channels}
    info = {"chosen": {cid: used.get(cid, srcs[0]) for cid, srcs in chosen.items()}, "errors": errors}
    return guide, info


def report(manifest: dict, guide: dict, info: dict) -> str:
    lines = ["# Programme guide report", "", f"Generated {guide['generated_at']}: "
             f"{len(guide['channels'])} of {len(manifest['channels'])} channels have programmes.", "",
             "| No. | Channel | Source | Programmes |", "| --- | --- | --- | --- |"]
    for ch in manifest["channels"]:
        cid = ch["id"]
        site = info["chosen"].get(cid)
        src = f"{site[0]} `{site[1]}`" if site else "no source in iptv-org index"
        n = len(guide["channels"].get(cid, []))
        lines.append(f"| {ch['number']} | {ch['name']} | {src} | {n or info['errors'].get(cid, '-')} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    config = generate.load_config()
    manifest = json.loads(generate.MANIFEST.read_text(encoding="utf-8"))
    index = json.loads(get(GUIDES_INDEX, 200_000_000))
    guide, info = build(config, manifest, index)
    covered = len(guide["channels"])
    print(f"guide: {covered}/{len(manifest['channels'])} channels, "
          f"{sum(len(v) for v in guide['channels'].values())} programmes")
    minimum = int(config.get("guide", {}).get("min_channels", 1))
    if covered < minimum:
        print(f"Only {covered} channels have programmes (minimum {minimum}); not publishing.", file=sys.stderr)
        return 1
    if not args.dry_run:
        GUIDE.parent.mkdir(parents=True, exist_ok=True)
        GUIDE.write_text(json.dumps(guide, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        REPORT.write_text(report(manifest, guide, info), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
