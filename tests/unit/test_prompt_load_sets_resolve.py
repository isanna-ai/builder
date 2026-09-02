"""Every asset a prompt declares in its `load_set` must exist after a real install.

An earlier fix covered the guardrail standards: three prompts declared
`standards/builder-guardrails-*.md` in every model tier while `asset-manifest.txt` and
`install.sh` between them installed none of the three. The test written alongside that fix
checked the MANIFEST, and only for `standards/` -- so it verified a proxy for the property, not
the property. The same defect class then turned up one asset type over:
`prompts/isanna-help.prompt.md` is declared in eight load_sets and landed only in the agent's
prompt directory, never under the `{{BUILDER_ROOT}}` root those declarations resolve against.

So this now does the only thing that actually settles it: run the real installer into a scratch
repo and resolve every declared path against the installed `.builder/`. Slower than reading the
manifest, and the only version that cannot be fooled by a copy loop that stages a file and then
writes it somewhere else.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"

# A load_set entry is a bare repo-relative path on its own list item.
_ENTRY = re.compile(r"^\s*-\s+((?:standards|skills|templates|schemas|prompts)/[A-Za-z0-9._/-]+)\s*$")


def _declared() -> set[str]:
    found: set[str] = set()
    for prompt in sorted(PROMPTS.glob("*.prompt.md")):
        for line in prompt.read_text(encoding="utf-8").splitlines():
            m = _ENTRY.match(line)
            if m:
                found.add(m.group(1))
    return found


def test_prompts_declare_load_set_assets_at_all():
    """Guard the guard: if the frontmatter shape changes and this regex stops matching, every
    assertion below would pass vacuously on an empty set."""
    declared = _declared()
    assert len(declared) >= 5, f"parsed only {len(declared)} load_set paths -- the parser is stale"
    assert any("guardrails" in d for d in declared), "expected the guardrail standards"
    assert any(d.startswith("prompts/") for d in declared), "expected at least one prompt asset"


def test_every_declared_load_set_asset_exists_in_the_repo():
    missing = sorted(d for d in _declared() if not (ROOT / d).is_file())
    assert missing == [], f"prompts declare load_set assets that do not exist: {missing}"


def test_every_declared_load_set_asset_resolves_after_a_real_install(tmp_path):
    """The property itself, checked the only way that proves it: install, then resolve.

    Every load_set path is written relative to `{{BUILDER_ROOT}}`, which
    `standards/builder-workflow.md` defines as the `.builder/` directory at the project root.
    A declaration that does not resolve there is a command whose first act is to read a file
    that is not present -- silently, because nothing else checks.
    """
    target = tmp_path / "install-target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    proc = subprocess.run(
        ["sh", str(ROOT / "install.sh"), "--target", str(target), "--yes"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert proc.returncode == 0, f"install failed:\n{proc.stdout}\n{proc.stderr}"

    builder_root = target / ".builder"
    missing = sorted(d for d in _declared() if not (builder_root / d).is_file())
    assert missing == [], (
        "these load_set assets do not resolve under the installed .builder/ root, so the "
        f"prompts that declare them load nothing: {missing}"
    )
