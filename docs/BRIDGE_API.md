# Bridge HTTP API

The reMark Bridge web service exposes a narrow HTTP API for external
clients — primarily the Obsidian companion plugin, but it's intentionally
small enough that scripts, Raycast/Alfred extensions, or a mobile
shortcut can use it too.

All `/api/*` endpoints require a bearer token. Issue and manage tokens
via the `remark-bridge bridge-token` CLI group; only the SHA-256 hash
is persisted, so a leaked state DB can't replay them.

```bash
remark-bridge bridge-token issue --label obsidian-laptop
#   Token for 'obsidian-laptop':
#     <copy this once — it won't be shown again>
```

## Authentication

Every call sets the header:

```
Authorization: Bearer <token>
```

Missing / malformed / revoked tokens return `401 Unauthorized` with
`WWW-Authenticate: Bearer`. The server compares token hashes in
constant time (`secrets.compare_digest`), so timing side channels
don't reveal valid prefixes.

## Endpoints

### `GET /api/status`

Returns the bridge's version, sync stats, and offline-queue summary.

**Response** (`200 OK`):

```json
{
  "version": "0.6.5",
  "client": "obsidian-laptop",
  "sync": {
    "total_docs": 142,
    "synced": 139,
    "errors": 1,
    "pending": 2,
    "last_sync": "2026-04-15T08:30:02+00:00"
  },
  "queue": {
    "pending": 2,
    "failed": 0,
    "done": 140
  }
}
```

`client` echoes the label you attached when issuing the token — useful
for debugging when multiple clients share a bridge.

### `POST /api/push`

Enqueues an Obsidian note for reverse-sync to the tablet.

**Request body**:

```json
{ "vault_path": "Projects/remarkable/refactor.md" }
```

`vault_path` is a path *relative to the vault root*. Absolute paths
and any path that escapes the vault root (`..`) are rejected with
`400`. Missing files return `404`.

**Response** (`200 OK`):

```json
{ "queued": true, "vault_path": "Projects/remarkable/refactor.md" }
```

Push itself is asynchronous: the reverse-sync worker picks the note
up on its next cycle. Re-POSTing the same path while it's still in
the queue is a safe no-op (`queued: false`).

## Error model

All `/api/*` failures follow FastAPI's default shape:

```json
{ "detail": "Invalid or revoked token" }
```

Production clients should treat any `5xx` response as transient
(retryable) and any `4xx` as terminal except `429` (rate limiting is
not yet enforced but may be in future releases).

## Revoking a client

```bash
remark-bridge bridge-token list
remark-bridge bridge-token revoke --id 3
```

The next request from that client returns `401`. Issued tokens are
never re-activated after revocation — issue a new one.
