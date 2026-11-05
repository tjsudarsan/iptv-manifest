"""One-off migration: build state/state.json from the TV's Live TV 1.1.0 diagnostics.

Input: the JSON array from `content call --uri content://com.gathin.iptvpoc.state --method channels`.
The diagnostics do not expose stream URLs, so for each channel the TV reports as available we
pick the first headerless source option that probes OK from here, else the first headerless option
(trusting the TV's own India-side check). Service IDs are copied exactly.

Usage: python -m tools.seed_from_tv TV_CHANNELS.json SOURCES_DIR
"""
import json
import sys
from pathlib import Path

from generator import core, generate, net


def main(tv_path: str, sources_dir: str) -> None:
    tv = json.loads(Path(tv_path).read_text())
    config = generate.load_config()
    layout = core.Layout(config)
    src = generate.Sources(config, Path(sources_dir))
    playlists = {l["id"]: src.playlist(l) for l in layout.languages}
    groups = generate.select(config, layout, playlists, {})
    hosts = frozenset(config["network"]["http_allowed_hosts"])
    streams, picked = {}, {"probed": 0, "trusted": 0}
    for row in tv:
        cid = row["id"]
        if row["excluded"]:
            continue
        if not row["available"]:
            streams[cid] = {"url": "", "available": False}
            continue
        options = [o for o in groups[cid][1] if not o.headers and core.stream_allowed(o.url, hosts)]
        # Prefer the option the TV chose: its name is the one the TV reports.
        options.sort(key=lambda o: o.name != row["name"])
        url = None
        for o in options:
            try:
                net.probe(o.url, http_hosts=hosts)
                url = o.url
                picked["probed"] += 1
                break
            except net.ProbeError:
                pass
        if url is None:
            url = options[0].url
            picked["trusted"] += 1
        streams[cid] = {"url": url, "available": True}
    state = {
        "version": 0,
        "content_hash": "",
        "service_ids": dict(sorted((r["id"], int(r["source_number"])) for r in tv)),
        "streams": dict(sorted(streams.items())),
        "source_counts": {lang: sum(1 for r in tv if r["language"] == lang) for lang in layout.lang_ids},
    }
    generate.write_json(generate.STATE, state)
    print(json.dumps(picked))


if __name__ == "__main__":
    main(*sys.argv[1:])
