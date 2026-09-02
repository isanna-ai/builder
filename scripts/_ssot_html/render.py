"""Render a product SSOT directory (YAML + Markdown) into one HTML page.

Design constraints, in priority order:

1. **Product-agnostic.** Nothing here knows what a capability, a journey or a
   constraint is. The renderer walks whatever YAML it is given. A schema-aware
   renderer would need editing every time a product invents a key, and would
   silently drop the keys it did not anticipate — dropping content from an SSOT
   is the one thing this tool must never do.
2. **Self-contained output.** Inline CSS, no scripts, no external fonts or
   images. The page has to survive being an email attachment, a Telegram
   document, or a strict-CSP artifact host.
3. **Deterministic.** Same input bytes produce the same output bytes. No
   timestamps unless `--stamp` asks for one, no dict reordering, no locale
   dependence. This is what makes a future `--check` mode possible.
4. **Zero dependencies** beyond builder's own `_yaml` shim.

The Markdown support is a deliberate subset (headings, lists, tables, fences,
blockquotes, hr, and inline emphasis/code/links). Anything outside it degrades
to a paragraph rather than being dropped or mangled.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - import shim mirrors the other builder scripts
    from _yaml import yaml
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _yaml import yaml  # type: ignore


class RenderError(Exception):
    """Raised for input problems the caller should report, not stack-trace."""


# The conventional reading order of an isanna product SSOT. Files present but
# unlisted are appended alphabetically, so a product that invents a new file
# still renders — just at the end, until someone gives it a place here.
DEFAULT_ORDER = (
    "README.md",
    "product.yaml",
    "intent-base.yaml",
    "constitution.md",
    "capabilities.yaml",
    "journeys.yaml",
    "domain-model.yaml",
    "system-behaviors.yaml",
    "integrations.yaml",
    "roadmap-coverage.yaml",
    "open-questions.yaml",
)

# Values that read as status anywhere in an SSOT get a badge. Unknown values
# render as plain text — the vocabulary is a presentation nicety, never a
# validation rule, so a product is free to invent its own words.
_BADGE_TONES: dict[str, str] = {
    # confidence / evidence
    "decided": "good",
    "evidenced": "good",
    "assumed": "warn",
    "proposed": "warn",
    "draft": "warn",
    "open": "warn",
    # lifecycle
    "shipped": "good",
    "as-built": "good",
    "building": "info",
    "in-spec": "info",
    "planned": "neutral",
    "to-be": "info",
    "dormant": "warn",
    "scaffold": "warn",
    "changed-by-roadmap": "info",
    "gap-unowned": "danger",
    "gap": "danger",
    "unguarded": "danger",
    # tiers / criticality
    "wedge": "good",
    "v1": "info",
    "later": "neutral",
    "never": "danger",
    "blocking": "danger",
    "core": "info",
    "enhancing": "neutral",
    "highest": "danger",
    # decisions
    "candidate": "info",
    "to-evaluate": "warn",
    "rejected": "danger",
    "unassigned": "warn",
    # risk
    "normal": "neutral",
    "destructive": "danger",
    "irreversible": "danger",
    # booleans
    "true": "info",
    "false": "neutral",
    "yes": "good",
    "no": "neutral",
    "partial": "warn",
}

# Keys whose value is a status-ish scalar worth badging. Keeping this to a key
# list (rather than badging every short string) stops prose from turning into a
# wall of pills.
_BADGE_KEYS = frozenset(
    {
        "confidence",
        "status",
        "state",
        "tier",
        "criticality",
        "decision",
        "change",
        "change_risk",
        "kind",
        "exists",
        "priority",
        "hard",
        "needs_verification",
        "implementation_spec",
        "artifact",
        "schema",
    }
)

# Keys that name a record, tried in order, when titling a card in a list.
_TITLE_KEYS = ("id", "name", "title", "question", "statement", "filter", "claim")


@dataclass
class Section:
    """One top-level key of one YAML document, or one heading run of Markdown."""

    anchor: str
    title: str
    body: str


@dataclass
class Document:
    """One file from the SSOT directory."""

    filename: str
    anchor: str
    title: str
    kind: str  # "yaml" | "markdown"
    sections: list[Section] = field(default_factory=list)
    lead: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return out or "x"


def _humanize(key: str) -> str:
    """`failure_conditions` -> `Failure conditions`. Ids stay as-is."""
    text = str(key).replace("_", " ").replace("-", " ").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _is_multiline(value: str) -> bool:
    return "\n" in value.strip()


def _badge(value: Any, tone: str | None = None) -> str:
    raw = str(value)
    tone = tone or _BADGE_TONES.get(raw.strip().lower(), "neutral")
    return f'<span class="badge badge-{tone}">{_esc(raw)}</span>'


# ---------------------------------------------------------------------------
# Inline Markdown (also used for prose inside YAML scalars)
# ---------------------------------------------------------------------------


def _inline(text: str) -> str:
    """Escape, then re-introduce the inline subset. Order matters: code first,
    so emphasis markers inside backticks are left alone."""
    out = _esc(text)

    placeholders: list[str] = []

    def _stash(html_fragment: str) -> str:
        placeholders.append(html_fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    out = re.sub(
        r"`([^`]+)`",
        lambda m: _stash(f"<code>{m.group(1)}</code>"),
        out,
    )
    # [label](target) — only http(s), mailto and in-page anchors are linked.
    out = re.sub(
        r"\[([^\]]+)\]\(((?:https?://|mailto:|#)[^)\s]+)\)",
        lambda m: _stash(f'<a href="{m.group(2)}" rel="noopener noreferrer">{m.group(1)}</a>'),
        out,
    )
    # Bare URLs.
    out = re.sub(
        r"(?<![\"\'>=])\bhttps?://[^\s<>\)\]]+",
        lambda m: _stash(f'<a href="{m.group(0)}" rel="noopener noreferrer">{m.group(0)}</a>'),
        out,
    )
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", out)

    for index, fragment in enumerate(placeholders):
        out = out.replace(f"\x00{index}\x00", fragment)
    return out


def _prose(text: str) -> str:
    """A YAML scalar that may span lines. Blank lines split paragraphs."""
    chunks = [c.strip() for c in re.split(r"\n\s*\n", str(text).strip()) if c.strip()]
    if not chunks:
        return ""
    return "".join(f"<p>{_inline(c)}</p>" for c in chunks)


# ---------------------------------------------------------------------------
# Markdown block parsing (the deliberate subset)
# ---------------------------------------------------------------------------


def _markdown_to_html(source: str) -> list[tuple[int, str, str]]:
    """Return (heading_level, heading_text, html) runs.

    Level 0 with an empty heading is the lead run before the first heading.
    """
    lines = source.replace("\r\n", "\n").split("\n")
    runs: list[tuple[int, str, list[str]]] = [(0, "", [])]
    index = 0
    total = len(lines)

    def emit(fragment: str) -> None:
        runs[-1][2].append(fragment)

    while index < total:
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        # Fenced code
        if stripped.startswith("```"):
            index += 1
            buffer: list[str] = []
            while index < total and not lines[index].strip().startswith("```"):
                buffer.append(lines[index])
                index += 1
            index += 1  # closing fence (or EOF)
            emit(f"<pre><code>{_esc(chr(10).join(buffer))}</code></pre>")
            continue

        # Headings
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            runs.append((level, text, []))
            index += 1
            continue

        # Horizontal rule
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            emit("<hr>")
            index += 1
            continue

        # Table: a header row followed by a separator row
        if (
            stripped.startswith("|")
            and index + 1 < total
            and re.fullmatch(r"\|[\s:\-|]+\|", lines[index + 1].strip() or "|")
        ):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            index += 2
            body_rows: list[list[str]] = []
            while index < total and lines[index].strip().startswith("|"):
                body_rows.append([c.strip() for c in lines[index].strip().strip("|").split("|")])
                index += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in header)
            rows_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in body_rows
            )
            emit(
                '<div class="table-wrap"><table><thead><tr>'
                + head_html
                + "</tr></thead><tbody>"
                + rows_html
                + "</tbody></table></div>"
            )
            continue

        # Blockquote
        if stripped.startswith(">"):
            buffer = []
            while index < total and lines[index].strip().startswith(">"):
                buffer.append(lines[index].strip().lstrip(">").strip())
                index += 1
            emit(f"<blockquote>{_prose(chr(10).join(buffer))}</blockquote>")
            continue

        # Lists (one level; nesting is flattened rather than dropped)
        list_match = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if list_match:
            ordered = not list_match.group(2) in ("-", "*", "+")
            items: list[str] = []
            while index < total:
                current = lines[index]
                item_match = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", current)
                if item_match:
                    items.append(item_match.group(3).strip())
                    index += 1
                    continue
                if current.strip() and current.startswith((" ", "\t")) and items:
                    items[-1] += " " + current.strip()
                    index += 1
                    continue
                break
            tag = "ol" if ordered else "ul"
            emit(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{tag}>")
            continue

        # Paragraph
        buffer = []
        while index < total and lines[index].strip() and not re.match(
            r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||```)", lines[index]
        ):
            buffer.append(lines[index].strip())
            index += 1
        if buffer:
            emit(f"<p>{_inline(' '.join(buffer))}</p>")
        else:
            # Nothing matched and nothing consumed — never spin.
            emit(f"<p>{_inline(stripped)}</p>")
            index += 1

    return [(level, text, "".join(parts)) for level, text, parts in runs]


# ---------------------------------------------------------------------------
# YAML rendering (generic, recursive)
# ---------------------------------------------------------------------------


def _render_value(key: str | None, value: Any, depth: int = 0) -> str:
    if value is None:
        return '<span class="nil">—</span>'

    if isinstance(value, bool):
        return _badge("true" if value else "false")

    if isinstance(value, (int, float)):
        return f'<span class="num">{_esc(value)}</span>'

    if isinstance(value, str):
        if key in _BADGE_KEYS and not _is_multiline(value) and len(value) <= 48:
            return _badge(value)
        if _is_multiline(value) or len(value) > 90:
            return _prose(value)
        return _inline(value)

    if isinstance(value, dict):
        return _render_mapping(value, depth + 1)

    if isinstance(value, list):
        if not value:
            return '<span class="nil">(vazio)</span>'
        if all(isinstance(item, (str, int, float, bool)) or item is None for item in value):
            items = "".join(f"<li>{_render_value(None, item, depth + 1)}</li>" for item in value)
            return f"<ul class='scalars'>{items}</ul>"
        return "".join(_render_card(item, depth + 1) for item in value)

    return _inline(str(value))


def _card_title(record: dict) -> tuple[str, str]:
    """Return (title, subtitle) for a record card."""
    title = ""
    for candidate in _TITLE_KEYS:
        raw = record.get(candidate)
        if isinstance(raw, str) and raw.strip():
            title = raw.strip()
            break
    subtitle = ""
    if title and isinstance(record.get("name"), str) and record["name"].strip() != title:
        subtitle = record["name"].strip()
    elif title and isinstance(record.get("title"), str) and record["title"].strip() != title:
        subtitle = record["title"].strip()
    return title, subtitle


def _render_card(item: Any, depth: int) -> str:
    if not isinstance(item, dict):
        return f'<div class="card">{_render_value(None, item, depth)}</div>'

    record = dict(item)
    title, subtitle = _card_title(record)

    # Pull the title key and any badge keys into the card header, so the body
    # is the substance rather than repeating the label.
    header_bits: list[str] = []
    if title:
        for candidate in _TITLE_KEYS:
            if record.get(candidate) == title:
                record.pop(candidate, None)
                break
        header_bits.append(f'<span class="card-title">{_inline(title)}</span>')
    if subtitle:
        for candidate in ("name", "title"):
            if record.get(candidate) == subtitle:
                record.pop(candidate, None)
                break
        header_bits.append(f'<span class="card-sub">{_inline(subtitle)}</span>')

    for badge_key in ("tier", "status", "state", "confidence", "criticality", "decision", "priority"):
        raw = record.get(badge_key)
        if isinstance(raw, str) and raw.strip():
            header_bits.append(_badge(raw))
            record.pop(badge_key)

    header = f'<div class="card-head">{"".join(header_bits)}</div>' if header_bits else ""
    body = _render_mapping(record, depth) if record else ""
    return f'<div class="card">{header}{body}</div>'


def _render_mapping(mapping: dict, depth: int) -> str:
    rows: list[str] = []
    for key, value in mapping.items():
        label = _humanize(key)
        rendered = _render_value(str(key), value, depth)
        block = isinstance(value, (dict, list)) or (isinstance(value, str) and _is_multiline(value))
        css = "row row-block" if block else "row"
        rows.append(
            f'<div class="{css}"><div class="k">{_esc(label)}</div>'
            f'<div class="v">{rendered}</div></div>'
        )
    return f'<div class="rows">{"".join(rows)}</div>'


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - surfaced as a caller-facing error
        raise RenderError(f"{path.name}: YAML does not parse ({exc})") from exc


def _document_from_yaml(path: Path, anchor: str) -> Document:
    data = _load_yaml(path)
    doc = Document(filename=path.name, anchor=anchor, title=path.name, kind="yaml")

    if data is None:
        doc.lead = '<p class="nil">(arquivo vazio)</p>'
        return doc

    if not isinstance(data, dict):
        doc.lead = _render_value(None, data, 0)
        return doc

    # Small scalar keys at the top (schema:, generated:, status:) read as
    # metadata, not as sections. Group them into a single meta strip.
    meta: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) and not (
            isinstance(value, str) and _is_multiline(value)
        ):
            meta[key] = value
        else:
            break

    if meta:
        chips = "".join(
            f'<span class="chip"><span class="chip-k">{_esc(_humanize(k))}</span>'
            f'<span class="chip-v">{_esc(v)}</span></span>'
            for k, v in meta.items()
        )
        doc.lead = f'<div class="meta">{chips}</div>'

    for key, value in data.items():
        if key in meta:
            continue
        doc.sections.append(
            Section(
                anchor=f"{anchor}--{_slug(key)}",
                title=_humanize(key),
                body=_render_value(str(key), value, 0),
            )
        )
    return doc


def _document_from_markdown(path: Path, anchor: str) -> Document:
    runs = _markdown_to_html(path.read_text(encoding="utf-8"))
    doc = Document(filename=path.name, anchor=anchor, title=path.name, kind="markdown")

    for level, heading, body in runs:
        if level == 0 and not heading:
            doc.lead += body
            continue
        if level == 1 and not doc.sections and not doc.lead.strip():
            # A single leading H1 is the document title, not a section.
            doc.title = heading
            doc.lead += body
            continue
        doc.sections.append(
            Section(
                anchor=f"{anchor}--{_slug(heading)}",
                title=heading,
                body=body,
            )
        )
    return doc


def collect_documents(ssot_dir: Path, order: list[str] | None = None) -> list[Document]:
    """Read every top-level .yaml/.yml/.md file in `ssot_dir`, in reading order.

    Subdirectories are ignored on purpose: an SSOT's reading order is a flat,
    curated sequence, and silently pulling in a nested `rebuild/` tree would
    bury it.
    """
    if not ssot_dir.is_dir():
        raise RenderError(f"not a directory: {ssot_dir}")

    found = {
        p.name: p
        for p in sorted(ssot_dir.iterdir())
        if p.is_file() and p.suffix.lower() in (".yaml", ".yml", ".md")
    }
    if not found:
        raise RenderError(f"no .yaml/.yml/.md files in {ssot_dir}")

    preferred = list(order) if order else list(DEFAULT_ORDER)
    ordered: list[str] = [name for name in preferred if name in found]
    ordered += [name for name in sorted(found) if name not in ordered]

    documents: list[Document] = []
    for name in ordered:
        path = found[name]
        anchor = f"f-{_slug(path.stem)}"
        if path.suffix.lower() == ".md":
            documents.append(_document_from_markdown(path, anchor))
        else:
            documents.append(_document_from_yaml(path, anchor))
    return documents


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#fbfaf8;--panel:#fff;--ink:#1c1b1f;--muted:#6b6870;--line:#e6e2dc;
--accent:#5b7f6c;--accent-ink:#2f4a3d;--code:#f2efe9;
--good:#2f6b4f;--good-bg:#e3f0e8;--warn:#8a6318;--warn-bg:#faf0dc;
--danger:#973341;--danger-bg:#fbe6e9;--info:#3a5c86;--info-bg:#e5edf7;
--neutral:#5c5960;--neutral-bg:#eeebe6;}
@media (prefers-color-scheme:dark){:root{--bg:#141317;--panel:#1b1a1f;--ink:#eceaf0;
--muted:#a09da8;--line:#2e2c34;--accent:#8fb8a1;--accent-ink:#b8d6c4;--code:#25232b;
--good:#8fd0ab;--good-bg:#1e3229;--warn:#e0b567;--warn-bg:#332a17;
--danger:#eb9aa6;--danger-bg:#382126;--info:#9cc0ea;--info-bg:#1d2a3a;
--neutral:#b6b3bc;--neutral-bg:#2a282f;}}
:root[data-theme=dark]{--bg:#141317;--panel:#1b1a1f;--ink:#eceaf0;--muted:#a09da8;
--line:#2e2c34;--accent:#8fb8a1;--accent-ink:#b8d6c4;--code:#25232b;
--good:#8fd0ab;--good-bg:#1e3229;--warn:#e0b567;--warn-bg:#332a17;
--danger:#eb9aa6;--danger-bg:#382126;--info:#9cc0ea;--info-bg:#1d2a3a;
--neutral:#b6b3bc;--neutral-bg:#2a282f;}
:root[data-theme=light]{--bg:#fbfaf8;--panel:#fff;--ink:#1c1b1f;--muted:#6b6870;
--line:#e6e2dc;--accent:#5b7f6c;--accent-ink:#2f4a3d;--code:#f2efe9;
--good:#2f6b4f;--good-bg:#e3f0e8;--warn:#8a6318;--warn-bg:#faf0dc;
--danger:#973341;--danger-bg:#fbe6e9;--info:#3a5c86;--info-bg:#e5edf7;
--neutral:#5c5960;--neutral-bg:#eeebe6;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.62 ui-sans-serif,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
-webkit-text-size-adjust:100%}
.wrap{max-width:64rem;margin:0 auto;padding:2rem 1.15rem 5rem}
header.top{border-bottom:1px solid var(--line);padding-bottom:1.4rem;margin-bottom:1.6rem}
h1{font-size:1.95rem;line-height:1.2;margin:0 0 .35rem;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:.95rem;margin:0}
nav.toc{background:var(--panel);border:1px solid var(--line);border-radius:.7rem;
padding:1rem 1.15rem;margin:0 0 2.2rem}
nav.toc h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;
color:var(--muted);margin:0 0 .6rem;font-weight:600}
nav.toc ol{margin:0;padding-left:1.15rem}
nav.toc li{margin:.22rem 0}
nav.toc a{color:var(--accent-ink);text-decoration:none}
nav.toc a:hover{text-decoration:underline}
nav.toc .file{font-weight:600}
nav.toc ul{list-style:none;margin:.2rem 0 .5rem;padding-left:.1rem}
nav.toc ul a{color:var(--muted);font-size:.88rem}
section.doc{margin:0 0 2.6rem;scroll-margin-top:1rem}
section.doc>h2{font-size:1.3rem;margin:0 0 .2rem;letter-spacing:-.01em;
padding-bottom:.45rem;border-bottom:2px solid var(--accent)}
.filename{font:600 .74rem/1 ui-monospace,SFMono-Regular,Menlo,monospace;
color:var(--muted);letter-spacing:.02em;display:block;margin:.5rem 0 1rem}
details.sec{border:1px solid var(--line);border-radius:.6rem;background:var(--panel);
margin:.7rem 0;overflow:hidden}
details.sec>summary{cursor:pointer;padding:.68rem .9rem;font-weight:600;font-size:.97rem;
list-style:none;display:flex;align-items:center;gap:.5rem}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary::before{content:"▸";color:var(--accent);font-size:.85rem;
transition:transform .12s ease;display:inline-block}
details.sec[open]>summary::before{transform:rotate(90deg)}
details.sec>summary:hover{background:var(--code)}
.sec-body{padding:.2rem .9rem 1rem;border-top:1px solid var(--line)}
.lead{margin-bottom:1rem}
p{margin:.6rem 0}
a{color:var(--accent-ink)}
code{background:var(--code);padding:.1rem .32rem;border-radius:.28rem;
font:.88em ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
pre{background:var(--code);padding:.85rem 1rem;border-radius:.5rem;overflow-x:auto}
pre code{background:none;padding:0}
blockquote{margin:.8rem 0;padding:.15rem 0 .15rem 1rem;border-left:3px solid var(--accent);
color:var(--muted)}
hr{border:0;border-top:1px solid var(--line);margin:1.4rem 0}
ul,ol{margin:.5rem 0;padding-left:1.3rem}
li{margin:.22rem 0}
.table-wrap{overflow-x:auto;margin:.9rem 0}
table{border-collapse:collapse;width:100%;font-size:.93rem}
th,td{border:1px solid var(--line);padding:.45rem .6rem;text-align:left;vertical-align:top}
th{background:var(--code);font-weight:600}
.meta{display:flex;flex-wrap:wrap;gap:.4rem;margin:0 0 1rem}
.chip{display:inline-flex;align-items:baseline;gap:.35rem;background:var(--panel);
border:1px solid var(--line);border-radius:1rem;padding:.16rem .62rem;font-size:.78rem}
.chip-k{color:var(--muted)}
.chip-v{font-weight:600}
.badge{display:inline-block;padding:.08rem .5rem;border-radius:1rem;font-size:.76rem;
font-weight:600;letter-spacing:.01em;white-space:nowrap}
.badge-good{color:var(--good);background:var(--good-bg)}
.badge-warn{color:var(--warn);background:var(--warn-bg)}
.badge-danger{color:var(--danger);background:var(--danger-bg)}
.badge-info{color:var(--info);background:var(--info-bg)}
.badge-neutral{color:var(--neutral);background:var(--neutral-bg)}
.rows{display:flex;flex-direction:column;gap:.1rem}
.row{display:grid;grid-template-columns:minmax(8.5rem,15.5rem) 1fr;gap:.2rem 1rem;
padding:.34rem 0;border-top:1px solid var(--line)}
.rows>.row:first-child{border-top:0}
.row-block{grid-template-columns:1fr}
.row-block>.k{margin-bottom:.15rem}
.k{color:var(--muted);font-size:.86rem;font-weight:600}
.v{min-width:0;overflow-wrap:anywhere}
.v>p:first-child{margin-top:0}
.v>p:last-child{margin-bottom:0}
ul.scalars{margin:.1rem 0;padding-left:1.15rem}
.card{border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:.5rem;background:var(--panel);padding:.7rem .85rem;margin:.55rem 0}
.card-head{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;margin-bottom:.4rem}
.card-title{font-weight:700;font-size:.98rem}
.card-sub{color:var(--muted);font-size:.9rem}
.card .card{background:var(--bg)}
.nil{color:var(--muted)}
.num{font-variant-numeric:tabular-nums}
footer.bot{margin-top:3rem;padding-top:1.2rem;border-top:1px solid var(--line);
color:var(--muted);font-size:.84rem}
@media (max-width:640px){
.wrap{padding:1.25rem .85rem 3.5rem}
h1{font-size:1.55rem}
.row{grid-template-columns:1fr;gap:.05rem}
.k{font-size:.8rem}
}
@media print{
body{background:#fff}
details.sec{break-inside:avoid}
details.sec>summary::before{content:""}
nav.toc{break-after:page}
}
"""


def _toc(documents: list[Document]) -> str:
    entries: list[str] = []
    for doc in documents:
        subs = "".join(
            f'<li><a href="#{s.anchor}">{_esc(s.title)}</a></li>' for s in doc.sections
        )
        sub_html = f"<ul>{subs}</ul>" if subs else ""
        entries.append(
            f'<li><a class="file" href="#{doc.anchor}">{_esc(doc.title)}</a>{sub_html}</li>'
        )
    return (
        '<nav class="toc"><h2>Conteúdo</h2><ol>' + "".join(entries) + "</ol></nav>"
    )


def _body(documents: list[Document], open_sections: bool) -> str:
    parts: list[str] = []
    for doc in documents:
        inner = [f'<section class="doc" id="{doc.anchor}">']
        inner.append(f"<h2>{_esc(doc.title)}</h2>")
        if doc.title != doc.filename:
            inner.append(f'<span class="filename">{_esc(doc.filename)}</span>')
        else:
            inner.append('<span class="filename">&nbsp;</span>')
        if doc.lead:
            inner.append(f'<div class="lead">{doc.lead}</div>')
        for section in doc.sections:
            attr = " open" if open_sections else ""
            inner.append(
                f'<details class="sec" id="{section.anchor}"{attr}>'
                f"<summary>{_esc(section.title)}</summary>"
                f'<div class="sec-body">{section.body}</div>'
                f"</details>"
            )
        inner.append("</section>")
        parts.append("".join(inner))
    return "".join(parts)


def render_page(
    documents: list[Document],
    *,
    title: str,
    subtitle: str = "",
    mode: str = "standalone",
    open_sections: bool = False,
    stamp: str = "",
    source_label: str = "",
) -> str:
    """Assemble the page. `mode` is "standalone" (a full HTML document) or
    "artifact" (a fragment: the Artifact host supplies doctype/head/body)."""
    if mode not in ("standalone", "artifact"):
        raise RenderError(f"unknown mode: {mode}")

    footer_bits = [b for b in (source_label, stamp) if b]
    footer = (
        f'<footer class="bot">{_esc(" · ".join(footer_bits))}</footer>' if footer_bits else ""
    )
    head_sub = f'<p class="sub">{_esc(subtitle)}</p>' if subtitle else ""

    content = (
        '<div class="wrap">'
        f'<header class="top"><h1>{_esc(title)}</h1>{head_sub}</header>'
        + _toc(documents)
        + _body(documents, open_sections)
        + footer
        + "</div>"
    )

    if mode == "artifact":
        return f"<title>{_esc(title)}</title>\n<style>{_CSS}</style>\n{content}\n"

    return (
        "<!doctype html>\n"
        '<html lang="pt-BR">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n{content}\n</body>\n</html>\n"
    )
