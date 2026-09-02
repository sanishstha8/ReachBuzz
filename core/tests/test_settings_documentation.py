"""
Every setting the application reads is documented.

Configuration drift is quiet. Someone adds ``env("NEW_THING")`` with a
plausible default, it works everywhere it is tested, and the first person to
deploy without that variable inherits the default without ever having been
offered the choice. Nothing fails; the system just behaves differently than
whoever deployed it believes.

``.env.example`` is the only place a deployer learns what knobs exist, so this
keeps it honest against the settings modules themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SETTINGS_MODULES = (
    BASE_DIR / "config" / "settings" / "base.py",
    BASE_DIR / "config" / "settings" / "production.py",
)
ENV_EXAMPLE = BASE_DIR / ".env.example"

# Read before the settings modules are imported, so they cannot appear in them.
BOOTSTRAP_ONLY = {"DJANGO_SETTINGS_MODULE"}

ENV_CALL = re.compile(r'env(?:\.\w+)?\(\s*["\'](\w+)["\']')
ENV_ASSIGNMENT = re.compile(r"^#?\s*(\w+)=", re.M)


def settings_variables() -> set[str]:
    names: set[str] = set()
    for module in SETTINGS_MODULES:
        names |= set(ENV_CALL.findall(module.read_text(encoding="utf-8")))
    return names - BOOTSTRAP_ONLY


def documented_variables() -> set[str]:
    return set(ENV_ASSIGNMENT.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def test_every_setting_the_app_reads_is_in_env_example() -> None:
    undocumented = sorted(settings_variables() - documented_variables())

    assert not undocumented, (
        "these are read by the settings modules but absent from .env.example, "
        f"so nobody deploying would know they exist: {undocumented}"
    )


def test_env_example_does_not_advertise_settings_that_are_not_read() -> None:
    """A documented variable nobody reads is a knob that silently does nothing."""
    unread = sorted(documented_variables() - settings_variables() - BOOTSTRAP_ONLY)

    assert not unread, f"documented in .env.example but read by nothing: {unread}"


@pytest.mark.parametrize(
    "secret",
    ["SECRET_KEY", "META_ACCESS_TOKEN", "META_APP_SECRET", "META_WEBHOOK_VERIFY_TOKEN"],
)
def test_no_credential_has_a_value_in_the_example(secret: str) -> None:
    """
    The example file is committed. A placeholder that looks like a real value
    is how a shared secret ends up in a repository and then in production
    because nobody changed it.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(rf"^{secret}=(.*)$", text, re.M)

    assert match is not None, f"{secret} is missing from .env.example"
    assert match.group(1).strip() == "", f"{secret} has a value in the committed example file"
