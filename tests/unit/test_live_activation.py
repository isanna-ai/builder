from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _builder_project_model.live_runtime import live_activation


def _home(*, enabled: bool, repos: tuple[str, ...]):
    return SimpleNamespace(policy=SimpleNamespace(governor_enabled=enabled, drain_repos=repos))


def test_live_activation_is_home_and_flag_and_allow_list_membership():
    assert live_activation(None, "alpha") is False
    assert live_activation(_home(enabled=False, repos=("alpha",)), "alpha") is False
    assert live_activation(_home(enabled=True, repos=()), "alpha") is False
    assert live_activation(_home(enabled=True, repos=("alpha",)), "beta") is False
    assert live_activation(_home(enabled=True, repos=("alpha",)), "alpha") is True
    assert live_activation(_home(enabled=True, repos=("alpha",))) is True
