# reMark Bridge — Obsidian plugin

Push the active Obsidian note to your reMarkable tablet with a single
command, and keep an eye on the bridge's sync status straight from the
status bar.

This plugin talks HTTP to a running [reMark
Bridge](https://github.com/BGGBTAC/reMark) instance (≥ v0.6.0), which
runs on your own machine / VPS / Docker stack. No data leaves your
infrastructure.

## Installation

### From the community plugin store (recommended)

1. In Obsidian: *Settings → Community plugins → Browse*.
2. Search for **reMark Bridge** and install it.
3. Enable the plugin.

### Manual install

1. Build `main.js` (`npm install && npm run build`) or download the
   pre-built release asset.
2. Copy `manifest.json`, `main.js`, and (if present) `styles.css`
   into `<your-vault>/.obsidian/plugins/remark-bridge/`.
3. Toggle the plugin on under *Settings → Community plugins*.

## Setup

1. On the server running reMark Bridge (v0.6.0+), issue a bearer
   token:

   ```bash
   remark-bridge bridge-token issue --label obsidian
   ```

   Copy the token — it's shown only once.

2. In Obsidian: *Settings → reMark Bridge*, paste the token and the
   URL where the bridge's web service is reachable (e.g.
   `http://localhost:8000`, or your Tailscale / ngrok / VPN hostname).

3. The status bar should flip from "reMark: …" to a live sync summary
   within 60 s. If you see "reMark: offline", double-check the URL
   and that the bridge server is running.

## Usage

| Action               | How                                                          |
|----------------------|--------------------------------------------------------------|
| Push the active note | Ribbon icon (`tablet`) · command `Push current note to reMarkable` |
| Refresh sync status  | Command `Refresh reMark Bridge status` · click the status bar    |
| Change server / token| *Settings → reMark Bridge*                                    |

Failed requests are retried with exponential back-off (default 3
attempts starting at 2 s). When all retries fail you get an Obsidian
notice with the underlying error — no silent failures.

## Development

```bash
npm install
npm run dev   # rebuilds main.js on change for Obsidian's live reload
```

## License

Non-commercial use under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/),
same as the bridge itself. Commercial licensing via the GitHub repo.
