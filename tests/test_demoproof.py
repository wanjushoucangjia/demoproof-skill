import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demoproof.cli import main
from demoproof.core import (DemoProofError, build, clean_controls, compile_extra,
                            read_transcript, render_svg, residual, sanitize,
                            target_dir, wrap_text)


class DemoProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "input.txt"
        self.source.write_text("$ tool --help\nusage: tool\n", encoding="utf-8")
        self.out = self.root / "demo"

    def build(self, **kwargs):
        options = dict(input_path=self.source, out=self.out, theme="dark", title="Demo",
                       width=80, output_format="svg", strict=False, patterns=[])
        options.update(kwargs)
        return build(**options)

    def test_basic_svg_render(self):
        self.build()
        self.assertIn(b"usage: tool", (self.out / "demo.svg").read_bytes())

    def test_empty_input(self):
        self.source.write_text("", encoding="utf-8")
        self.build()
        self.assertTrue((self.out / "demo.svg").exists())

    def test_unicode_chinese(self):
        self.source.write_text("你好 CLI\n", encoding="utf-8")
        self.build()
        self.assertIn("你好", (self.out / "demo.svg").read_text(encoding="utf-8"))

    def test_windows_path_redacted(self):
        safe, counts = sanitize("C:\\Users\\Alice\\work")
        self.assertNotIn("Alice", safe)
        self.assertEqual(counts["home-path"], 1)

    def test_unix_home_redacted(self):
        safe, _ = sanitize("/home/alice/file")
        self.assertNotIn("alice", safe)

    def test_ansi_and_osc_removed(self):
        self.assertEqual(clean_controls("\x1b[31mred\x1b[0m \x1b]0;title\x07ok"), "red ok")

    def test_controls_removed(self):
        self.assertEqual(clean_controls("a\x00\x1fb"), "ab")

    def test_svg_escape(self):
        self.source.write_text('<script>alert("x")</script>', encoding="utf-8")
        self.build(title='<img src="x">')
        svg = (self.out / "demo.svg").read_text(encoding="utf-8")
        self.assertNotIn("<script>", svg)
        self.assertNotIn("<img", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_html_escape(self):
        self.source.write_text("<script>alert(1)</script>", encoding="utf-8")
        self.build(output_format="html")
        page = (self.out / "demo.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", page)

    def test_html_mode_no_svg_file(self):
        self.build(output_format="html")
        self.assertFalse((self.out / "demo.svg").exists())

    def test_both_formats(self):
        self.build(output_format="both")
        self.assertTrue((self.out / "demo.svg").exists())
        self.assertTrue((self.out / "demo.html").exists())

    def test_light_theme(self):
        svg = render_svg(["text"], "Demo", "light")
        self.assertIn(b'#ffffff', svg)

    def test_width_wrap(self):
        self.assertGreater(len(wrap_text("x" * 50, 20)), 1)

    def test_bad_width(self):
        with self.assertRaises(DemoProofError):
            wrap_text("x", 121)

    def test_oversize_input(self):
        self.source.write_bytes(b"a" * 1_000_001)
        with self.assertRaises(DemoProofError):
            read_transcript(self.source)

    def test_long_line(self):
        self.source.write_text("a" * 4001, encoding="utf-8")
        with self.assertRaises(DemoProofError):
            read_transcript(self.source)

    def test_invalid_utf8(self):
        self.source.write_bytes(b"\xff")
        with self.assertRaises(DemoProofError):
            read_transcript(self.source)

    def test_path_traversal(self):
        with self.assertRaises(DemoProofError):
            target_dir(str(self.root / "safe" / ".." / "escape"))

    def test_relative_output_rejected(self):
        with self.assertRaises(DemoProofError):
            target_dir("dist/demo")

    def test_credential_redacted(self):
        safe, _ = sanitize("api_key=abcdefghijklmnop")
        self.assertNotIn("abcdefghijklmnop", safe)

    def test_email_redacted(self):
        safe, _ = sanitize("me@example.com")
        self.assertNotIn("me@example.com", safe)

    def test_custom_pattern(self):
        safe, _ = sanitize("internal-id-123", compile_extra([r"internal-id-\d+"]))
        self.assertEqual(safe, "[REDACTED]")

    def test_invalid_pattern(self):
        with self.assertRaises(DemoProofError):
            compile_extra(["["])

    def test_strict_mode_rejects_match(self):
        self.source.write_text("me@example.com", encoding="utf-8")
        with self.assertRaises(DemoProofError):
            self.build(strict=True)

    def test_residual_scan(self):
        self.assertIn("email", residual("me@example.com"))

    def test_deterministic_outputs(self):
        self.build()
        first = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.build()
        self.assertEqual(first, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_manifest_hashes(self):
        manifest = self.build()
        import hashlib
        for name, digest in manifest["outputs"].items():
            self.assertEqual(hashlib.sha256((self.out / name).read_bytes()).hexdigest(), digest)

    def test_provenance(self):
        manifest = self.build()
        self.assertEqual(manifest["source"], "supplied transcript")
        self.assertIs(manifest["executed"], False)
        self.assertIsNone(manifest["exit_code"])

    def test_no_subprocess_or_network(self):
        with patch("subprocess.run", side_effect=AssertionError("execution")), patch("socket.socket", side_effect=AssertionError("network")):
            self.build()

    def test_cli_check(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["check", "--input", str(self.source)]), 0)
        self.assertIn("supplied transcript", output.getvalue())

    def test_cli_error_exit(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["check", "--input", str(self.root / "missing")]), 2)

    def test_cli_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("render", output.getvalue())

    def test_example_runs(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "sample-transcript.txt"
        self.assertEqual(main(["render", "--input", str(example), "--out", str(self.out)]), 0)
        self.assertTrue((self.out / "demo-manifest.json").exists())

    def test_committed_example_is_reproducible(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        build(examples / "sample-transcript.txt", self.out, "dark", "Hello CLI demo", 80,
              "both", False, [])
        for expected in (examples / "expected-output").iterdir():
            self.assertEqual((self.out / expected.name).read_bytes(), expected.read_bytes(), expected.name)


if __name__ == "__main__":
    unittest.main()
