from pathlib import Path
import shutil

from unittest import SkipTest

import planning
from _builder_project_model.dispatch_plan import build_dispatch_plan
from _builder_project_model.home import load_builder_home
from tests.unit.public_export_support import require_live_spec_corpus


def test_release_intents_flatten_in_original_order():
    root = Path(__file__).resolve().parents[2]
    require_live_spec_corpus(root, "the release-intent flatten-order invariant")
    inventory, _ = planning.intent_inventory(root)
    visible_by_id = {item.intent.intent: item for item in inventory if item.intent is not None}

    release = planning.find_release(root, "builder-behavioral-ssot")
    assert release is not None
    if not planning.release_uses_intents(release.status):
        raise SkipTest(
            "builder-behavioral-ssot is shipped (historical/spec-based); the intent flatten-order "
            "invariant applies only while it is unshipped/intent-based."
        )
    flattened = []
    for intent_id in release.intents:
        flattened.extend(list(visible_by_id[intent_id].intent.specs))

    assert flattened == [
        "ssot-builder-home",
        "ssot-governor-sessions",
        "ssot-scheduler-draining",
        "ssot-authoring-cutover",
    ]


def test_builder_home_flattens_intents_for_dispatch_in_authored_order(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "builder_project_model" / "home" / "portfolio"
    portfolio = tmp_path / "portfolio"
    shutil.copytree(fixture, portfolio)
    for repo_name in ("alpha-repo", "beta-repo", "shared-repo"):
        (portfolio / repo_name / ".git").mkdir()

    alpha_repo = portfolio / "alpha-repo"
    original_intent = (
        alpha_repo / ".builder" / "intents" / "alpha-release-work" / "intent.yaml"
    ).read_text(encoding="utf-8")
    zeta_intent = (
        original_intent
        .replace("alpha-release-work", "zeta-release-work")
        .replace("Alpha release work", "Zeta release work")
        .replace("  - alpha-core\n  - shared/shared-spec\n", "  - alpha-late\n")
    )
    zeta_path = alpha_repo / ".builder" / "intents" / "zeta-release-work" / "intent.yaml"
    zeta_path.parent.mkdir(parents=True)
    zeta_path.write_text(zeta_intent, encoding="utf-8")
    release_path = (
        portfolio
        / ".builder-home"
        / "projects"
        / "alpha"
        / "releases"
        / "alpha-release.yaml"
    )
    release_path.write_text(
        release_path.read_text(encoding="utf-8").replace(
            "intents:\n  - alpha-release-work\n",
            "intents:\n  - zeta-release-work\n  - alpha-release-work\n",
        ),
        encoding="utf-8",
    )

    home = load_builder_home(portfolio / ".builder-home")
    alpha = home.project("alpha")
    assert alpha is not None
    release = alpha.releases[0].declaration
    assert release.intents == ("zeta-release-work", "alpha-release-work")
    assert [member.spec for member in release.specs] == [
        "alpha-late",
        "alpha-core",
        "shared/shared-spec",
    ]

    actions = build_dispatch_plan(home=home, project_id="alpha", release_name="alpha-release")
    assert [action.ref for action in actions] == [
        "alpha-late",
        "alpha-core",
        "shared/shared-spec",
    ]
    assert [action.roadmap_index for action in actions] == [0, 1, 2]
