#!/usr/bin/env python3
"""Bootstrap a demo state DB + vault for the screenshot workflow.

Thin wrapper around :func:`src.web.demo.seed`: lays down a throwaway
``config.yaml`` pointing at a tempdir, then invokes the seeder.

Usage (from the repo root)::

    python scripts/seed_demo_data.py [--out .demo]

The script writes three artifacts under ``--out`` (default ``.demo``):
``config.yaml``, ``vault/`` and ``state/state.db``. Set
``REMARK_CONFIG=<out>/config.yaml`` before starting the web server.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from src.config import AppConfig
from src.web import demo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=".demo",
        help="Directory to write config.yaml + vault + state (default: .demo)",
    )
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    vault = out / "vault"
    state_dir = out / "state"
    vault.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    cfg = AppConfig()
    cfg.obsidian.vault_path = str(vault)
    cfg.sync.state_db = str(state_dir / "state.db")
    cfg.logging.file = str(out / "bridge.log")
    cfg.web.app_name = "reMark Demo"

    # Write a minimal config.yaml so the CLI / web server can load it
    # via REMARK_CONFIG — model_dump_json → yaml keeps every default.
    config_path = out / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    demo.seed(cfg)

    print(f"Demo seeded at {out}")
    print(f"  REMARK_CONFIG={config_path}")
    print(f"  vault:        {vault}")
    print(f"  state_db:     {cfg.sync.state_db}")


if __name__ == "__main__":
    main()
