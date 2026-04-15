# Publishing to the Obsidian community store

The store pulls plugins from **their own GitHub repos**, not from
monorepo subdirectories. To ship this plugin:

## 1. Split into a dedicated repo

Create a new repo at `github.com/BGGBTAC/obsidian-remark-bridge` and
copy this folder's contents (except `COMMUNITY_STORE.md`) to the root.
The commit history in this monorepo stays intact; the dedicated repo
starts fresh:

```bash
# From the reMark monorepo root
cp -r contrib/obsidian-plugin /tmp/obsidian-remark-bridge
cd /tmp/obsidian-remark-bridge
rm COMMUNITY_STORE.md
git init -b main
git add .
git commit -m "Initial import from reMark v0.6.0"
gh repo create BGGBTAC/obsidian-remark-bridge --public \
    --description "Push notes to reMarkable via reMark Bridge" --source . --push
```

## 2. Build and tag a release

The community store fetches `main.js` + `manifest.json` from a GitHub
release, not from the tree. Every release tag must be an **exact
version string without a `v` prefix** (Obsidian's reviewer bot rejects
tags like `v0.1.0`).

```bash
npm install
npm run build                 # produces main.js (minified)
git tag 0.1.0 -m "0.1.0"
git push origin 0.1.0

gh release create 0.1.0 \
    manifest.json main.js \
    --title "0.1.0" \
    --notes "Initial release."
```

## 3. Submit to the community-plugins list

Obsidian's store is backed by a JSON file in
`obsidianmd/obsidian-releases`. Fork the repo and append this block to
`community-plugins.json`:

```json
{
  "id": "remark-bridge",
  "name": "reMark Bridge",
  "author": "BGGBTAC",
  "description": "Push notes to a reMarkable tablet and monitor your reMark Bridge sync status.",
  "repo": "BGGBTAC/obsidian-remark-bridge"
}
```

Open a pull request titled exactly `Add reMark Bridge` and fill in
their checklist template. Typical review turnaround: a few days.

## Release checklist (every version)

- [ ] Bump `manifest.json` `version` and `versions.json` entry
- [ ] `npm run build` and inspect the diff in `main.js`
- [ ] Tag the exact version (no `v` prefix) and push
- [ ] Attach `manifest.json` + `main.js` to the GitHub release
- [ ] Update the "Latest" line in this folder's README if users need
      a new bridge minimum version
