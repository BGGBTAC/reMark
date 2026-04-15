# Upgrading reMark Bridge

This page covers every breaking change and migration step between
recent releases. The state DB is **always** additive — upgrading never
requires a reindex, but some features unlock new behavior you may
want to opt into.

## → 0.6.5 (service release)

**Nothing to do.** Patch release: security fixes, performance wins,
documentation cleanup. `pip install -U remark-bridge` (or
`docker compose pull`) is sufficient.

If you were relying on:

- `systemd/remarkable-bridge.service` — **removed**. Replace with the
  split `remark-bridge-sync.service` + `remark-bridge-web.service`
  units that have been recommended since 0.3.1. See the
  "Deployment (systemd)" section in the README.
- Bearer tokens — every existing token still works. Token lookup is
  now constant-time; no user action required.

## → 0.6.0 ("Ecosystem")

New features, no breaking changes.

- **Templates** grew `extends:` / `blocks:` / `when:`. Existing flat
  templates render identically.
- **Bridge HTTP API** is opt-in: no API endpoints exist until you
  issue the first bridge token.
- **Obsidian companion plugin** lives at `contrib/obsidian-plugin/`
  and gets installed separately inside your vault.

## → 0.5.0 ("Integrations & Smart")

**New dependency**: `ruamel.yaml >= 0.18` for the web settings
editor. Docker and PyPI pull this automatically; pip-installed source
checkouts need `pip install -U -e .[dev]`.

**New state DB table**: `sync_queue`. Created automatically on first
start — no manual migration needed.

**Opt-in features** you may want to turn on:

- `processing.hierarchical_tags: true` — switches the tagger prompt to
  produce slash-separated tags. **Only affects new notes** — run
  `remark-bridge retag --dry-run` to preview how existing notes would
  change, then re-run without `--dry-run` to backfill.
- Web `/settings/<section>` now edits YAML in place (preserving inline
  comments).
- `notion.enabled: true` + `NOTION_TOKEN` env var + a shared parent
  page id — mirrors every synced note into your Notion workspace.

**Removed**: nothing.

## → 0.4.0 ("Distribution & Multi-Device")

**Database migration** (automatic, additive):

- `sync_state` gains a `device_id` column defaulting to `'default'`
  for every pre-0.4 row.
- New `devices` registry table.

**Single-tablet installs**: no action required. Keep
`remarkable.devices: []` in `config.yaml` and the sync loop behaves
exactly as it did in 0.3.x.

**Multi-tablet installs**: register each tablet and then add a
matching entry under `remarkable.devices`:

```bash
remark-bridge device add --id pro --label "Paper Pro" --subfolder rm-pro
remark-bridge auth --device pro
# ... edit config.yaml to add the device under remarkable.devices ...
remark-bridge sync --once
```

**Search**: hybrid BM25+semantic is now the default (`search.mode:
hybrid`). If you relied on the old semantic-only behavior, set
`search.mode: semantic` explicitly.

**Docker**: the shipped `docker-compose.yml` now pulls from
`ghcr.io/bggbtac/remark-bridge:${REMARK_IMAGE_TAG:-latest}`. Users who
built locally before still can — the `build:` block is a fallback.

## → 0.3.1 (reliability patch)

- License metadata on PyPI switched to CC-BY-NC-4.0. Existing 0.3.0
  wheels still show MIT (PyPI releases are immutable). Upgrade to
  pick up correct metadata.
- `systemd/` units were split into `-sync` and `-web` variants. The
  combined `remarkable-bridge.service` stayed available in 0.3.x but
  is **removed in 0.6.5** — migrate now if you haven't.
- `REMARK_LOG_FORMAT=json` env var opts into structured logging.

## → 0.3.0 ("Dashboard, PWA, plugins")

First release with a web UI, plugin system, reverse sync, and
template engine. No breaking changes from 0.2; every new feature is
off by default.

## Troubleshooting upgrades

- **`sqlite3.OperationalError: no such column: ...`** — you're running
  an old bridge binary against a newer state DB. Pin the CLI to the
  same version or downgrade the DB by restoring from backup.
- **Pydantic validation errors on startup** — a config key was
  renamed or removed. Compare your `config.yaml` against
  `config.example.yaml` from the version you're upgrading to.
- **Web UI shows "No token"** for the Obsidian plugin after an
  upgrade — bridge tokens are preserved across upgrades; re-check
  the plugin's server URL and `Authorization: Bearer` header.
