"""Initial policy digests approved through SKCapstone's legacy review gates."""

from types import MappingProxyType
from typing import Mapping

# Registry changes require a separately reviewed SKCapstone legacy-gated change.
INITIAL_PROFILE_DIGESTS: Mapping[str, str] = MappingProxyType(
    {
        "https://github.com/smilinTux/skgateway": (
            "f48fc610962a8da11d1d673665ac4d369dc6f28d43c745c9cc52f6b122bdb24b"
        ),
    }
)

# Review cards born before immutable CI profiles may use only these exact
# repository-wide hosted check names. Registry changes require independent
# review and cannot be supplied through mutable card links.
LEGACY_CI_CHECKS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "https://github.com/smilinTux/skgateway": (
            "test (22)",
            "docs / docs-check",
            "gitleaks",
        ),
    }
)
