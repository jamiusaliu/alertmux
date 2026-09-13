"""Fixtures reach the tests byte-for-byte as committed.

Every fixture is committed with LF line endings, and the adapter tests edit
them with exact byte replacements that span a newline. A checkout that rewrites
line endings -- ``core.autocrlf=true``, the Git for Windows default -- turns
those replacements into silent no-ops, so a test asserting on the edited feed
fails with an unrelated-looking assertion instead. ``.gitattributes`` marks the
fixtures ``-text``; this names the cause if that protection is ever lost.
"""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("path", sorted(FIXTURES.iterdir()), ids=lambda path: path.name)
def test_fixture_has_no_carriage_returns(path):
    assert b"\r" not in path.read_bytes(), (
        f"{path.name} contains carriage returns, so the checkout rewrote its line "
        "endings. Keep tests/fixtures byte-exact in .gitattributes, then re-checkout: "
        "git rm -r --cached tests/fixtures && git checkout -- tests/fixtures"
    )
