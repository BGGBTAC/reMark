#!/usr/bin/env python3
"""Capture wiki screenshots via headless Chromium (Playwright).

Drives the demo-mode web server and dumps PNGs into ``--out``. The CI
workflow pairs this with ``seed_demo_data.py`` and the ``screenshots``
action; for local runs start a demo server yourself first::

    python scripts/seed_demo_data.py --out .demo
    REMARK_CONFIG=.demo/config.yaml REMARK_DEMO_MODE=1 \\
        uvicorn src.web.app:create_app --factory --host 127.0.0.1 --port 8000 &
    python scripts/screenshots.py --base http://127.0.0.1:8000 --out screenshots
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote


# Tuple of (filename, path, options). ``full_page`` captures below the
# fold when the route's content is longer than the viewport.
SHOTS: list[tuple[str, str, dict]] = [
    ("dashboard",           "/",                                         {"full_page": True}),
    ("notes-list",          "/notes",                                    {"full_page": True}),
    ("note-detail",         "/notes/" + quote("rm-pro/Meetings/meeting-2026-03-12.md"),
                                                                         {"full_page": True}),
    ("ask",                 "/ask",                                      {}),
    ("actions",             "/actions",                                  {}),
    ("queue",               "/queue",                                    {"full_page": True}),
    ("queue-failed",        "/queue?status=failed",                      {}),
    ("devices",             "/devices",                                  {}),
    ("settings-index",      "/settings",                                 {}),
    ("settings-processing", "/settings/processing",                      {"full_page": True}),
    ("settings-notion",     "/settings/notion",                          {}),
    ("settings-search",     "/settings/search",                          {}),
    ("templates-list",      "/templates",                                {}),
    ("templates-edit",      "/templates/meeting",                        {"full_page": True}),
    ("quick-entry",         "/quick-entry",                              {}),
    ("users-page",          "/users",                                    {"full_page": True}),
    ("audit",               "/audit",                                    {"full_page": True}),
    ("reports",             "/reports",                                  {"full_page": True}),
    ("login",               "/login",                                    {}),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default="screenshots")
    parser.add_argument(
        "--width", type=int, default=1440,
        help="Viewport width (retina scaling is applied via device_scale_factor=2)",
    )
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright not installed. Run:\n"
            "  pip install playwright && playwright install --with-deps chromium",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=2,
        )
        page = ctx.new_page()

        # v0.7+ routes gate on a session cookie. In demo mode the
        # bridge seeds an `admin` user whose password comes from
        # REMARK_ADMIN_PASSWORD (set by the workflow); if the env is
        # missing we still try to render the login + public pages.
        admin_pw = os.environ.get("REMARK_ADMIN_PASSWORD", "")
        if admin_pw:
            page.goto(args.base.rstrip("/") + "/login", wait_until="networkidle")
            page.fill('input[name="username"]', "admin")
            page.fill('input[name="password"]', admin_pw)
            page.click('button[type="submit"]')
            page.wait_for_url(args.base.rstrip("/") + "/", timeout=10_000)

        failed: list[str] = []
        for name, path, opts in SHOTS:
            url = args.base.rstrip("/") + path
            try:
                page.goto(url, wait_until="networkidle", timeout=15_000)
                # Small settle window — Alpine/HTMX sometimes finish a
                # tick after networkidle fires.
                page.wait_for_timeout(400)
                page.screenshot(path=str(out_dir / f"{name}.png"), **opts)
                print(f"  ✓ {name:<22} {url}")
            except Exception as exc:  # noqa: BLE001
                failed.append(name)
                print(f"  ✗ {name:<22} {url}  ({exc})", file=sys.stderr)

        browser.close()

    if failed:
        print(f"\n{len(failed)} shot(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\n{len(SHOTS)} screenshots → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
