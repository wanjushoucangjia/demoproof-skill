"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import DemoProofError, build, compile_extra, read_transcript, residual, sanitize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demoproof", description="Render a supplied terminal transcript without executing it")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "render"):
        command = sub.add_parser(name)
        command.add_argument("--input", required=True, type=Path)
        command.add_argument("--redact-pattern", action="append", default=[])
        command.add_argument("--strict-redaction", action="store_true")
        if name == "render":
            command.add_argument("--out", required=True, type=Path)
            command.add_argument("--theme", choices=("dark", "light"), default="dark")
            command.add_argument("--title", default="Terminal demo")
            command.add_argument("--width", type=int, default=80)
            command.add_argument("--format", choices=("svg", "html", "both"), default="svg")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            raw, _ = read_transcript(args.input)
            extra = compile_extra(args.redact_pattern)
            cleaned, counts = sanitize(raw, extra)
            if residual(cleaned, extra):
                raise DemoProofError("Residual sensitive pattern")
            count = sum(counts.values())
            if args.strict_redaction and count:
                raise DemoProofError("Strict redaction rejected sensitive input")
            print(f"DemoProof: {count} redaction(s); supplied transcript; manual review required")
        else:
            manifest = build(args.input, args.out, args.theme, args.title, args.width,
                             args.format, args.strict_redaction, args.redact_pattern)
            print(f"DemoProof: wrote {len(manifest['outputs']) + 1} files to {args.out}; no command executed")
        return 0
    except (DemoProofError, OSError) as exc:
        print(f"DemoProof error: {exc}", file=sys.stderr)
        return 2
