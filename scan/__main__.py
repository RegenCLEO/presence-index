"""Command line: ``python -m scan <stage> --panel panel.yaml --out DIR``."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from scan import panel as panel_file
from scan.engines import EngineConfigError
from scan.store import StoreError

STAGES = {
    "volumes": "scan.volumes",
    "run": "scan.run",
    "keywords": "scan.keywords",
    "siteread": "scan.siteread",
    "reviews": "scan.reviews",
    "mentions": "scan.mentions",
    "local": "scan.local",
    "parse": "scan.parse",
    "score": "scan.score",
    "rank": "scan.rank",
    "detail": "scan.detail",
    "sensitivity": "scan.sensitivity",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scan", description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)
    freeze = sub.add_parser("freeze", help="write the panel's SHA-256 into the panel file")
    freeze.add_argument("panel", type=Path)
    for name, module_name in STAGES.items():
        module = importlib.import_module(module_name)
        stage = sub.add_parser(name, help=(module.__doc__ or "").strip().splitlines()[0])
        stage.add_argument("--panel", required=True, type=Path)
        stage.add_argument("--out", required=True, type=Path, help="directory for this scan's files")
        module.add_arguments(stage)
        stage.set_defaults(handler=module.main)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.stage == "freeze":
            print(panel_file.freeze(args.panel))
            return 0
        return args.handler(args)
    except (panel_file.PanelError, StoreError, EngineConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
