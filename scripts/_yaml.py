"""Prefer PyYAML, with the bundled compatibility parser as a zero-dependency fallback."""

from __future__ import annotations

try:
    import yaml as yaml
except ImportError:
    import _yaml_compat as yaml
else:
    from enum import StrEnum

    def _represent_str_enum(dumper, value):
        """Keep StrEnum records scalar when real PyYAML is installed."""
        return dumper.represent_str(str(value))

    # PyYAML's SafeDumper does not inherit the str representer for StrEnum.
    # Register only that type: plain Enum and IntEnum must fail rather than be
    # silently coerced to misleading strings.
    yaml.SafeDumper.add_multi_representer(StrEnum, _represent_str_enum)
