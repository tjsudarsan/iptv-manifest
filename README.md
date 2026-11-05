# Live TV channel manifest

Backend for the owner's private **Live TV** app (`com.gathin.iptvpoc`) on a Sony BRAVIA.
The TV does not parse playlists or filter channels any more: it downloads one manifest,
validates it, and registers exactly those channels with Sony's TV app.

```
config/channels.toml ──► generator (GitHub Actions, every 6 h + on push) ──► docs/v1/manifest.json
                                         ▲                                        │
iptv-org Tamil/English M3U + metadata ───┘                                        ▼
                                                    TV: GitHub Pages URL, raw.githubusercontent.com fallback
```

- **Published URL:** `https://tjsudarsan.github.io/iptv-manifest/v1/manifest.json`
  (fallback `https://raw.githubusercontent.com/tjsudarsan/iptv-manifest/main/docs/v1/manifest.json`)
- **Human-readable list:** [`reports/latest.md`](reports/latest.md)

## Changing channels

Edit `config/channels.toml` and push to `main` (or ask Claude). The workflow tests the generator,
regenerates the manifest and publishes it. The TV picks it up on its next check
(`refresh_minutes`, or **Update source** in the app's settings).

| Task | How |
| --- | --- |
| Remove a channel | add its base ID to `policy.excluded_ids`, or `overrides."ID".disabled = true` |
| Rename / change logo | `overrides."ID".name` / `.logo` |
| Move to another row | `overrides."ID".category = "ta-news"` |
| Force a stream URL | `overrides."ID".url = "https://…/master.m3u8"` |
| Keep listed but unplayable | `overrides."ID".unavailable = "reason"` |
| Add a channel not in iptv-org | a `[[manual]]` entry |
| Add an English channel | add its base ID to an `en-*` category's `ids` |
| Reorder within a row | `pinned` on the category |
| Add a language | a `[[languages]]` entry plus its categories |

## Guarantees the TV relies on

- `id` is a channel's permanent identity; `service_id` never changes or gets reused
  (`state/state.json` keeps the history).
- Display `number`s are contiguous in language → category → pinned → name order.
- `version` increases only when published content changes; the TV ignores older or equal versions.
- Every configured category is listed, in row order, even when empty.
- A failed, truncated or invalid source keeps the previous manifest (the run fails, nothing is published).
- Streams: an unchanged, previously working URL is kept; changed URLs are probed (HLS playlist + one
  media sample, no auth headers, no encryption); if every option fails, the previous working URL stays.

Probes run from GitHub's servers (outside India). A stream that works there may be geo-blocked
in India; mark such channels with `overrides."ID".unavailable` until they are verified on the TV.

## Schema (v1)

```json
{
  "schema": 1, "version": 1, "generated_at": "2026-11-05T03:56:46Z",
  "min_app_version": 19, "refresh_minutes": 360,
  "languages":  [{"id": "ta", "name": "Tamil", "audio": ["ta", "tam"]}],
  "categories": [{"id": "ta-music", "language": "ta", "name": "Music"}],
  "channels":   [{"id": "IsaiAruvi.in@SD", "service_id": 9538, "number": "1", "name": "Isai Aruvi (396p)",
                  "language": "ta", "category": "ta-music", "logo": "https://…", "url": "https://…m3u8"}],
  "unavailable": [{"id": "…", "name": "…", "language": "ta", "category": "ta-other"}]
}
```

## Local use

```sh
python -m unittest discover -s tests -t .
python -m generator.generate            # fetches sources, probes changed streams, writes files
python -m generator.generate --dry-run  # print the summary only
```

Python 3.11+ standard library only. `tools/seed_from_tv.py` is the one-off migration that copied the
TV's existing service IDs and working streams (Live TV 1.1.0) into `state/state.json`.
