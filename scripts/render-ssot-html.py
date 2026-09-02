#!/usr/bin/env python3
"""Render a product SSOT directory into one self-contained HTML page.

  render-ssot-html.py <ssot-dir> [--out PATH] [--mode standalone|artifact]

Product-agnostic: it walks whatever YAML the directory holds rather than
knowing any particular schema, so it renders any two products'
`docs/ssot/<product>/` directories alike. Output is self-contained (inline CSS, no scripts, no
external assets) and deterministic — same input bytes, same output bytes,
unless --stamp asks for a timestamp.

Modes:
  standalone  a full HTML document; for a file, an attachment, a browser.
  artifact    a fragment (title + style + content, no doctype/html/head/body)
              for the Artifact publisher, which supplies the skeleton.

Exit codes: 0 ok · 1 input error · 2 usage
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ssot_html import RenderError, collect_documents, render_page  # noqa: E402


def _default_title(ssot_dir: Path, documents) -> str:
    """Prefer the product's declared title, then its id, then the dir name."""
    for doc in documents:
        if doc.filename == "product.yaml":
            path = ssot_dir / "product.yaml"
            try:
                from _yaml import yaml  # noqa: PLC0415

                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                break
            if isinstance(data, dict):
                for key in ("title", "product"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return f"{value.strip()} — SSOT"
            break
    return f"{ssot_dir.name} — SSOT"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a product SSOT directory into one self-contained HTML page.",
    )
    parser.add_argument("ssot_dir", nargs="?", help="Directory holding the SSOT (.yaml/.md files)")
    parser.add_argument("--out", help="Write here instead of stdout")
    parser.add_argument(
        "--mode",
        default="standalone",
        choices=["standalone", "artifact"],
        help="standalone = full HTML document; artifact = fragment for the Artifact publisher",
    )
    parser.add_argument("--title", help="Page title (default: from product.yaml, else the dir name)")
    parser.add_argument("--subtitle", default="", help="One line under the title")
    parser.add_argument(
        "--order",
        help="Comma-separated filenames to lead the reading order; the rest follow alphabetically",
    )
    parser.add_argument(
        "--open", action="store_true", dest="open_sections",
        help="Render sections expanded (default: collapsed)",
    )
    parser.add_argument(
        "--stamp", default="",
        help="Footer text, e.g. a date. Omit to keep output byte-deterministic.",
    )
    args = parser.parse_args()

    if not args.ssot_dir:
        parser.print_help()
        return 2

    ssot_dir = Path(args.ssot_dir).resolve()
    order = [o.strip() for o in args.order.split(",") if o.strip()] if args.order else None

    try:
        documents = collect_documents(ssot_dir, order)
        html = render_page(
            documents,
            title=args.title or _default_title(ssot_dir, documents),
            subtitle=args.subtitle,
            mode=args.mode,
            open_sections=args.open_sections,
            stamp=args.stamp,
            source_label=f"{len(documents)} arquivos · {ssot_dir.name}/",
        )
    except RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        print(f"wrote {out_path} ({len(html):,} bytes, {len(documents)} documents)", file=sys.stderr)
    else:
        sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
