from __future__ import annotations

import html
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from unittest import SkipTest

# NOTE: the repo's `python3 -m pytest` resolves to the local shim in pytest/,
# which provides only the tmp_path fixture — no `pytest.raises`, no `skip`.
# Hence the try/except idiom below, matching the other unit tests.

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _ssot_html import RenderError, collect_documents, render_page  # noqa: E402

VOID_TAGS = {"meta", "br", "hr", "img", "link", "input"}


def _write_fixture(root: Path) -> Path:
    ssot = root / "ssot" / "demo"
    ssot.mkdir(parents=True)
    (ssot / "product.yaml").write_text(
        "product: demo\ntitle: Demo Product\nrepos:\n  - alias: demo-repo\n",
        encoding="utf-8",
    )
    (ssot / "intent-base.yaml").write_text(
        "schema: demo/v1\n"
        "status: draft\n"
        "idea:\n"
        "  one_liner: Uma frase com acento e <tag> perigosa\n"
        "  confidence: decided\n"
        "constraints:\n"
        "  - id: C1\n"
        "    statement: Deve sempre valer isto\n"
        "  - id: C2\n"
        "    statement: E tambem isto\n"
        "non_goals:\n"
        "  - Primeiro nao-objetivo\n"
        "  - Segundo nao-objetivo\n",
        encoding="utf-8",
    )
    (ssot / "constitution.md").write_text(
        "# Demo Constitution\n\n"
        "Some lead prose.\n\n"
        "## Money\n\n"
        "- Integer cents, **never** float\n"
        "- No `third-party` funds\n\n"
        "| Rule | Id |\n|---|---|\n| Cents | C1 |\n\n"
        "> A quoted warning\n",
        encoding="utf-8",
    )
    return ssot


def _assert_well_formed(markup: str) -> None:
    stack: list[str] = []
    problems: list[str] = []

    class Checker(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in VOID_TAGS:
                stack.append(tag)

        def handle_endtag(self, tag):
            if tag in VOID_TAGS:
                return
            if not stack or stack[-1] != tag:
                problems.append(f"</{tag}> closes {stack[-1] if stack else 'nothing'}")
            elif stack:
                stack.pop()

    Checker().feed(markup)
    assert not problems, problems
    assert not stack, f"unclosed tags: {stack}"


def test_renders_both_modes_well_formed(tmp_path):
    ssot = _write_fixture(tmp_path)
    documents = collect_documents(ssot)

    standalone = render_page(documents, title="Demo", mode="standalone")
    assert standalone.startswith("<!doctype html>")
    _assert_well_formed(standalone)

    fragment = render_page(documents, title="Demo", mode="artifact")
    # The Artifact host supplies the skeleton; emitting our own would nest
    # a second document inside its <body>. Match on a tag boundary — a bare
    # "<head" substring also hits the legitimate "<header>".
    for forbidden in (r"<!doctype", r"<html[\s>]", r"<head[\s>]", r"<body[\s>]"):
        assert not re.search(forbidden, fragment, re.I), forbidden
    assert "<title>Demo</title>" in fragment
    _assert_well_formed(fragment)


def test_reading_order_is_conventional_then_alphabetical(tmp_path):
    ssot = _write_fixture(tmp_path)
    (ssot / "zzz-extra.yaml").write_text("later: true\n", encoding="utf-8")
    (ssot / "aaa-extra.yaml").write_text("early: true\n", encoding="utf-8")

    names = [d.filename for d in collect_documents(ssot)]
    # Known files lead, in DEFAULT_ORDER; unknown ones follow alphabetically.
    assert names[:3] == ["product.yaml", "intent-base.yaml", "constitution.md"]
    assert names[3:] == ["aaa-extra.yaml", "zzz-extra.yaml"]


def test_explicit_order_overrides_the_default(tmp_path):
    ssot = _write_fixture(tmp_path)
    names = [d.filename for d in collect_documents(ssot, ["constitution.md"])]
    assert names[0] == "constitution.md"


def test_no_yaml_content_is_dropped(tmp_path):
    """The property that matters: rendering an SSOT must not lose any of it.

    A schema-aware renderer would silently skip keys it did not anticipate.
    This asserts every scalar leaf reaches the page.
    """
    ssot = _write_fixture(tmp_path)
    markup = render_page(collect_documents(ssot), title="Demo", mode="standalone")

    leaves = [
        "Uma frase com acento e <tag> perigosa",
        "Deve sempre valer isto",
        "E tambem isto",
        "Primeiro nao-objetivo",
        "Segundo nao-objetivo",
        "demo-repo",
        "C1",
        "C2",
    ]
    for leaf in leaves:
        assert html.escape(leaf, quote=True) in markup, f"dropped: {leaf}"


def test_markup_in_content_is_escaped(tmp_path):
    ssot = _write_fixture(tmp_path)
    markup = render_page(collect_documents(ssot), title="Demo", mode="standalone")
    assert "<tag>" not in markup
    assert "&lt;tag&gt;" in markup


def test_markdown_subset_renders(tmp_path):
    ssot = _write_fixture(tmp_path)
    doc = next(d for d in collect_documents(ssot) if d.filename == "constitution.md")
    # A single leading H1 becomes the document title, not a section.
    assert doc.title == "Demo Constitution"
    bodies = "".join(s.body for s in doc.sections)
    assert "<li>" in bodies
    assert "<strong>never</strong>" in bodies
    assert "<code>third-party</code>" in bodies
    assert "<table>" in bodies
    assert "<blockquote>" in bodies


def test_output_is_deterministic(tmp_path):
    ssot = _write_fixture(tmp_path)
    first = render_page(collect_documents(ssot), title="Demo", mode="standalone")
    second = render_page(collect_documents(ssot), title="Demo", mode="standalone")
    assert first == second
    # ...and carries no timestamp unless one is asked for.
    stamped = render_page(
        collect_documents(ssot), title="Demo", mode="standalone", stamp="2026-07-29"
    )
    assert "2026-07-29" in stamped and "2026-07-29" not in first


def test_page_is_self_contained(tmp_path):
    """No external fetch of any kind: the page must survive a strict CSP,
    an email attachment and an offline phone alike."""
    ssot = _write_fixture(tmp_path)
    markup = render_page(collect_documents(ssot), title="Demo", mode="standalone")
    assert "<script" not in markup.lower()
    for external in ("src=", "@import", "url(http", "<link"):
        assert external not in markup.lower(), external


def test_badges_only_for_known_status_keys(tmp_path):
    ssot = _write_fixture(tmp_path)
    markup = render_page(collect_documents(ssot), title="Demo", mode="standalone")
    assert 'class="badge badge-good">decided<' in markup


def _expect_render_error(fn, label: str) -> None:
    try:
        fn()
    except RenderError:
        return
    raise AssertionError(f"expected RenderError for {label}")


def test_input_errors_are_reported_not_raised_raw(tmp_path):
    _expect_render_error(
        lambda: collect_documents(tmp_path / "does-not-exist"), "missing directory"
    )

    empty = tmp_path / "empty"
    empty.mkdir()
    _expect_render_error(lambda: collect_documents(empty), "directory with no documents")

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "product.yaml").write_text("a:\n  - b\n c: [\n", encoding="utf-8")
    _expect_render_error(lambda: collect_documents(broken), "unparseable YAML")


def test_unknown_mode_rejected(tmp_path):
    ssot = _write_fixture(tmp_path)
    documents = collect_documents(ssot)
    _expect_render_error(
        lambda: render_page(documents, title="Demo", mode="pdf"), "unknown mode"
    )


def test_renders_a_real_ssot_directory_when_one_is_offered():
    """Smoke test against a real SSOT of another shape, when one is offered.

    Point ``BUILDER_SMOKE_SSOT_DIRS`` at one or more real SSOT directories (os.pathsep
    separated) to exercise the renderer against documents this repo does not contain. It
    used to probe two hardcoded sibling checkouts and `return` when they were absent --
    which reported PASS in every bare clone while asserting nothing at all, the exact
    green-by-omission this project refuses. Absent input is now an explicit skip.
    """
    offered = os.environ.get("BUILDER_SMOKE_SSOT_DIRS", "")
    live = [Path(p) for p in offered.split(os.pathsep) if p and Path(p).is_dir()]
    if not live:
        raise SkipTest(
            "no real SSOT directory offered; set BUILDER_SMOKE_SSOT_DIRS to exercise this"
        )
    for ssot in live:
        markup = render_page(collect_documents(ssot), title=ssot.name, mode="standalone")
        assert len(markup) > 5000
        _assert_well_formed(markup)
