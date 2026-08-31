"""Render docs/project-history.html to a print-ready PDF.

`docs/project-history.html` is authored as an Artifact body — it carries no
`<!doctype>`/`<html>`/`<head>` wrapper, because the Artifact host supplies one.
This script adds a standalone wrapper plus print-only CSS (drops the sticky
nav rail, keeps feature cards and tables off page breaks) and drives headless
Chrome to produce the PDF.

    py tools/make_pdf.py
    py tools/make_pdf.py --out somewhere/else.pdf

The wrapper is written to a temporary file and removed afterwards, so the HTML
stays the single source of truth and the PDF cannot drift from it silently.
Requires Chrome or Edge; nothing else in this repo depends on either.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "docs" / "project-history.html"
DEFAULT_OUT = REPO / "docs" / "Shopping-Copilot-Development-Record.pdf"

BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{color-scheme:light}
  html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  body{margin:0}
  img{max-width:100%}
  [hidden]{display:none!important}
  @page{size:A4;margin:16mm 14mm}
  @media print{
    .rail{display:none!important}
    .cols{display:block!important}
    main{max-width:none!important}
    header.hero{padding-top:0!important}
    section{margin-bottom:34px!important;break-inside:auto}
    section>h2,h3,h4{break-after:avoid}
    .feat,.note,.act,figure,.chart-box,.pipe-step,.mods{break-inside:avoid}
    .tw{break-inside:avoid;overflow-x:visible!important}
    table{font-size:8.5pt}
    body{font-size:10.5pt;background:#fff!important}
    pre{white-space:pre-wrap;break-inside:avoid}
    a{text-decoration:none}
    footer{break-before:avoid}
  }
</style>
</head>
<body>
"""
FOOT = "\n</body>\n</html>\n"


def find_browser() -> str:
    for name in ("chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    raise SystemExit(
        "No Chrome or Edge found. Install one, or open "
        f"{SOURCE} in a browser and print to PDF manually."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output PDF path")
    args = ap.parse_args()

    if not SOURCE.exists():
        raise SystemExit(f"missing source: {SOURCE}")

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    browser = find_browser()

    body = SOURCE.read_text(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="pdfsrc-")) / "print.html"
    tmp.write_text(HEAD + body + FOOT, encoding="utf-8")

    try:
        subprocess.run(
            [
                browser,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                # Google Fonts are fetched over the network; the budget lets
                # them land before the snapshot. Without a network the
                # declared fallback stacks are used and the render still works.
                "--virtual-time-budget=25000",
                "--run-all-compositor-stages-before-draw",
                "--print-to-pdf-no-header",
                f"--print-to-pdf={out}",
                tmp.as_uri(),
            ],
            check=False,
            capture_output=True,
            timeout=180,
        )
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    if not out.exists() or out.stat().st_size < 10_000:
        raise SystemExit("PDF was not produced; try printing from a browser instead.")

    head = out.read_bytes()[:5]
    if head != b"%PDF-":
        raise SystemExit(f"output is not a PDF (starts {head!r})")

    print(f"wrote {out}  ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
