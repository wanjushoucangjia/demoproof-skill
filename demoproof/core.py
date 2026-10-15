"""Read, sanitize, and render supplied transcripts. Never execute them."""

from __future__ import annotations

import hashlib
import html
import json
import re
import textwrap
from pathlib import Path

from . import REDACTION_VERSION, VERSION

MAX_BYTES = 1_000_000
MAX_LINE = 4_000
MAX_LINES = 2_000
MAX_WIDTH = 120
MAX_RENDER_CHARS = 120_000

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")
RULES = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)),
    ("credential", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)\b(\s*[:=]\s*)[\"']?[^\s\"',;]{8,}")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("home-path", re.compile(r"(?i)(?:C:\\Users\\|/Users/|/home/)[^\\/\s]+")),
)


class DemoProofError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_transcript(path: Path) -> tuple[str, str]:
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise DemoProofError("Input must be a file of at most 1,000,000 bytes")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise DemoProofError("Input exceeds size limit")
    try:
        value = raw.decode("utf-8")
    except UnicodeError as exc:
        raise DemoProofError("Input must be UTF-8 text") from exc
    lines = value.splitlines()
    if len(lines) > MAX_LINES or any(len(line) > MAX_LINE for line in lines):
        raise DemoProofError("Input exceeds line limit")
    return value, sha256(raw)


def clean_controls(value: str) -> str:
    value = ANSI.sub("", value)
    return "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32 and ord(ch) != 127)


def compile_extra(patterns: list[str]) -> list[re.Pattern[str]]:
    compiled = []
    for pattern in patterns:
        if len(pattern) > 200 or not pattern:
            raise DemoProofError("Redaction pattern must be 1–200 characters")
        try:
            candidate = re.compile(pattern)
        except re.error as exc:
            raise DemoProofError("Invalid redaction pattern") from exc
        if candidate.search(""):
            raise DemoProofError("Redaction pattern may not match empty text")
        compiled.append(candidate)
    return compiled


def sanitize(value: str, extra: list[re.Pattern[str]] | None = None) -> tuple[str, dict[str, int]]:
    value = clean_controls(value)
    counts: dict[str, int] = {}
    for name, pattern in RULES:
        value, count = pattern.subn("[REDACTED]", value)
        counts[name] = count
    for i, pattern in enumerate(extra or [], 1):
        value, count = pattern.subn("[REDACTED]", value)
        counts[f"custom-{i}"] = count
    return value, counts


def residual(value: str, extra: list[re.Pattern[str]] | None = None) -> list[str]:
    return [name for name, pattern in RULES if pattern.search(value)] + [
        f"custom-{i}" for i, pattern in enumerate(extra or [], 1) if pattern.search(value)
    ]


def wrap_text(value: str, width: int) -> list[str]:
    if not 20 <= width <= MAX_WIDTH:
        raise DemoProofError("Width must be between 20 and 120")
    output = []
    for line in value.split("\n"):
        output.extend(textwrap.wrap(line.expandtabs(4), width=width, break_long_words=True,
                                    break_on_hyphens=False, replace_whitespace=False,
                                    drop_whitespace=False) or [""])
    if len(output) > MAX_LINES or sum(map(len, output)) > MAX_RENDER_CHARS:
        raise DemoProofError("Rendered transcript exceeds limit")
    return output


def render_svg(lines: list[str], title: str, theme: str) -> bytes:
    colors = {"dark": ("#111827", "#e5e7eb"), "light": ("#ffffff", "#111827")}
    if theme not in colors:
        raise DemoProofError("Unknown theme")
    background, foreground = colors[theme]
    width = 900
    height = 64 + 22 * len(lines)
    if height > 45_000:
        raise DemoProofError("SVG height exceeds limit")
    title_xml = html.escape(title, quote=True)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{title_xml}">',
             f'<rect width="{width}" height="{height}" fill="{background}"/>',
             f'<text x="24" y="32" fill="{foreground}" font-family="monospace" font-size="18">{title_xml}</text>']
    for i, line in enumerate(lines):
        parts.append(f'<text x="24" y="{62 + i * 22}" fill="{foreground}" font-family="monospace" font-size="15" xml:space="preserve">{html.escape(line, quote=True)}</text>')
    parts.append("</svg>\n")
    return "\n".join(parts).encode("utf-8")


def render_html(svg: bytes, title: str) -> bytes:
    escaped_title = html.escape(title, quote=True)
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>{escaped_title}</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            f'<body>{svg.decode("utf-8")}</body></html>\n').encode("utf-8")


def target_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DemoProofError("--out must be an absolute directory without '..'")
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise DemoProofError("--out must be a directory")
    return path


def build(input_path: Path, out: Path, theme: str, title: str, width: int,
          output_format: str, strict: bool, patterns: list[str]) -> dict:
    if len(title) > 120:
        raise DemoProofError("Title exceeds 120 characters")
    extra = compile_extra(patterns)
    raw, input_hash = read_transcript(input_path)
    safe, counts = sanitize(raw, extra)
    safe_title, title_counts = sanitize(title, extra)
    if strict and (sum(counts.values()) or sum(title_counts.values())):
        raise DemoProofError("Strict redaction rejected sensitive input")
    lines = wrap_text(safe, width)
    svg = render_svg(lines, safe_title, theme)
    files: dict[str, bytes] = {"demo.svg": svg}
    if output_format in ("html", "both"):
        files["demo.html"] = render_html(svg, safe_title)
    if output_format == "html":
        del files["demo.svg"]
    primary = "demo.html" if output_format == "html" else "demo.svg"
    markdown_title = re.sub(r"([\\`*_{}\[\]()#+.!>|~-])", r"\\\1", safe_title).replace("\n", " ")
    files["demo.md"] = (f"# {markdown_title}\n\nSupplied transcript; no command was executed.\n\n"
                         f"![{markdown_title}]({primary})\n").encode("utf-8")
    total_counts = {name: count + title_counts.get(name, 0) for name, count in counts.items()}
    report = {"rule_version": REDACTION_VERSION, "redaction_counts": total_counts,
              "manual_review_required": True, "residual_rule_matches": []}
    files["redaction-report.json"] = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    for data in files.values():
        if residual(data.decode("utf-8"), extra):
            raise DemoProofError("Residual sensitive pattern in output")
    reproducible = ("python -m demoproof render --input <transcript> --out <absolute-directory> "
                    f"--theme {theme} --width {width} --format {output_format} "
                    f"--title {json.dumps(safe_title, ensure_ascii=False)}")
    if strict:
        reproducible += " --strict-redaction"
    if patterns:
        reproducible += " --redact-pattern <same-private-pattern>" * len(patterns)
    manifest = {"tool_version": VERSION, "input_sha256": input_hash,
                "source": "supplied transcript", "executed": False, "exit_code": None,
                "redaction_rule_version": REDACTION_VERSION, "mode": output_format,
                "theme": theme, "width": width,
                "reproduce": reproducible,
                "outputs": {name: sha256(data) for name, data in sorted(files.items())}}
    files["demo-manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    out = target_dir(str(out))
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return manifest
