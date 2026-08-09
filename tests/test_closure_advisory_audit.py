"""Unit tests for the closure advisory audit (R13-F1 hardening).

Background
----------

The ``dependency-audit`` CI job runs ``pip-audit`` against ``uv.lock`` — the
dev / ``pip install`` resolution universe. The DEPLOYED artifact is built by
``nix/packages.nix`` (``buildPythonApplication``) entirely from the pinned
nixpkgs channel and never reads ``uv.lock``. The two resolution universes
drift: a version CI marks clean can ship vulnerable, and vice-versa.

Live proof of the drift on the current pin (2026-08-09): the shipping closure
carries ``pydantic-settings 2.12.0`` (GHSA-4xgf-cpjx-pc3j) and
``cryptography 49.0.0`` (CVE-2026-69247 / GHSA-g6cj-pr64-35w5 /
PYSEC-2026-3552) while ``uv.lock`` pins versions past both fixes — so the
uv.lock audit is green while the deploy is not.

``nix/audit/closure_advisory_audit.py`` closes that gap: it resolves the
ACTUAL nix-closure python package versions and runs ``pip-audit`` against
THAT set, reconciled against a checked-in advisory allowlist so the gate is
actionable (red == a NEW unaccepted advisory in the shipping closure, not
"nixpkgs has CVEs"). These tests exercise the pure parse / reconcile logic —
no nix build, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO_ROOT / "nix" / "audit"
ALLOWLIST = AUDIT_DIR / "closure-advisory-allowlist.toml"

sys.path.insert(0, str(AUDIT_DIR))

import closure_advisory_audit as audit

# ── store-path parsing ────────────────────────────────────────────────

# A representative slice of `nix-store -qR .#agent-mcp` output: the two
# vulnerable packages, a hyphenated name, a calver, and the interpreter
# derivation (which must NOT be mistaken for a package).
SAMPLE_REQUISITES = [
    "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-python3.14-pydantic-settings-2.12.0",
    "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-python3.14-cryptography-49.0.0",
    "/nix/store/cccccccccccccccccccccccccccccccc-python3.14-argon2-cffi-bindings-25.1.0",
    "/nix/store/dddddddddddddddddddddddddddddddd-python3.14-certifi-2026.06.17",
    "/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-python3-3.14.6",
    "/nix/store/ffffffffffffffffffffffffffffffff-glibc-2.40-66",
]


def test_parse_python_packages_extracts_versions() -> None:
    pkgs = audit.parse_python_packages(SAMPLE_REQUISITES)
    assert pkgs["pydantic-settings"] == "2.12.0"
    assert pkgs["cryptography"] == "49.0.0"
    # Hyphenated name splits correctly at the version boundary.
    assert pkgs["argon2-cffi-bindings"] == "25.1.0"
    # Calendar version survives.
    assert pkgs["certifi"] == "2026.06.17"


def test_parse_python_packages_excludes_interpreter_and_non_python() -> None:
    pkgs = audit.parse_python_packages(SAMPLE_REQUISITES)
    # `python3-3.14.6` (the interpreter) has no dotted `python3.N-` prefix.
    assert "python3" not in pkgs
    assert not any("glibc" in k for k in pkgs)


def test_requirements_text_is_pinned_and_deduped() -> None:
    text = audit.requirements_text({"cryptography": "49.0.0", "anyio": "4.14.2"})
    lines = sorted(text.strip().splitlines())
    assert lines == ["anyio==4.14.2", "cryptography==49.0.0"]


# ── pip-audit JSON parsing ────────────────────────────────────────────

# Shape emitted by `pip-audit --format json`: cryptography reported with a
# GHSA primary id and PYSEC/CVE aliases; pydantic-settings with a bare GHSA.
PIP_AUDIT_JSON = {
    "dependencies": [
        {"name": "anyio", "version": "4.14.2", "vulns": []},
        {
            "name": "cryptography",
            "version": "49.0.0",
            "vulns": [
                {
                    "id": "GHSA-g6cj-pr64-35w5",
                    "fix_versions": ["50.0.0"],
                    "aliases": ["CVE-2026-69247", "PYSEC-2026-3552"],
                    "description": "Bleichenbacher oracle in PKCS#7 decrypt.",
                }
            ],
        },
        {
            "name": "pydantic-settings",
            "version": "2.12.0",
            "vulns": [
                {
                    "id": "GHSA-4xgf-cpjx-pc3j",
                    "fix_versions": ["2.14.2"],
                    "aliases": [],
                    "description": "secrets_dir symlink escape.",
                }
            ],
        },
    ]
}


def test_parse_pip_audit_json_collects_ids_and_aliases() -> None:
    advisories = audit.parse_pip_audit_json(PIP_AUDIT_JSON)
    by_pkg = {a.package: a for a in advisories}
    assert set(by_pkg) == {"cryptography", "pydantic-settings"}
    crypto = by_pkg["cryptography"]
    # Primary id and every alias are queryable for matching.
    assert "GHSA-g6cj-pr64-35w5" in crypto.ids
    assert "PYSEC-2026-3552" in crypto.ids
    assert "CVE-2026-69247" in crypto.ids


# ── reconciliation ────────────────────────────────────────────────────


def test_empty_allowlist_flags_every_advisory() -> None:
    found = audit.parse_pip_audit_json(PIP_AUDIT_JSON)
    unaccepted, stale = audit.reconcile(found, [])
    assert {a.package for a in unaccepted} == {"cryptography", "pydantic-settings"}
    assert stale == []


def test_allowlist_by_primary_id_accepts() -> None:
    found = audit.parse_pip_audit_json(PIP_AUDIT_JSON)
    allow = [
        audit.AllowEntry(
            id="GHSA-4xgf-cpjx-pc3j",
            package="pydantic-settings",
            aliases=(),
            rationale="unreachable",
        )
    ]
    unaccepted, stale = audit.reconcile(found, allow)
    # cryptography still unaccepted; pydantic-settings cleared.
    assert {a.package for a in unaccepted} == {"cryptography"}
    assert stale == []


def test_allowlist_matches_by_alias() -> None:
    """An entry keyed on PYSEC clears an advisory pip-audit keyed on GHSA."""
    found = audit.parse_pip_audit_json(PIP_AUDIT_JSON)
    allow = [
        audit.AllowEntry(
            id="PYSEC-2026-3552",  # pip-audit reported GHSA as primary
            package="cryptography",
            aliases=(),
            rationale="unreachable",
        )
    ]
    unaccepted, _ = audit.reconcile(found, allow)
    assert {a.package for a in unaccepted} == {"pydantic-settings"}


def test_allowlist_does_not_cross_packages() -> None:
    """A matching id under the wrong package name does not silence anything."""
    found = audit.parse_pip_audit_json(PIP_AUDIT_JSON)
    allow = [
        audit.AllowEntry(
            id="GHSA-4xgf-cpjx-pc3j",
            package="cryptography",  # wrong package for this id
            aliases=(),
            rationale="typo",
        )
    ]
    unaccepted, stale = audit.reconcile(found, allow)
    assert {a.package for a in unaccepted} == {"cryptography", "pydantic-settings"}
    # The entry matched nothing -> stale.
    assert len(stale) == 1


def test_stale_allowlist_entry_is_flagged() -> None:
    """An accepted advisory no longer present in the closure must be removed."""
    found = audit.parse_pip_audit_json(PIP_AUDIT_JSON)
    allow = [
        audit.AllowEntry(
            id="GHSA-4xgf-cpjx-pc3j",
            package="pydantic-settings",
            aliases=(),
            rationale="unreachable",
        ),
        audit.AllowEntry(
            id="GHSA-dead-beef-0000",
            package="left-pad",
            aliases=(),
            rationale="retired advisory nobody removed",
        ),
    ]
    unaccepted, stale = audit.reconcile(found, allow)
    assert {a.package for a in unaccepted} == {"cryptography"}
    assert [e.package for e in stale] == ["left-pad"]


# ── the committed allowlist ───────────────────────────────────────────


def test_committed_allowlist_parses() -> None:
    entries = audit.load_allowlist(ALLOWLIST.read_text())
    assert entries, "the allowlist must have at least the seeded entries"
    for e in entries:
        assert e.id and e.package and e.rationale, (
            "every allowlist entry needs an id, a package, and a one-line "
            f"rationale — got {e!r}"
        )


def test_committed_allowlist_makes_the_known_divergence_green_and_honest() -> None:
    """The seeded allowlist accepts today's real closure advisories and nothing
    stale — proving the gate CATCHES the divergence (they are found) while
    staying green (they are accepted with rationale)."""
    entries = audit.load_allowlist(ALLOWLIST.read_text())
    found = audit.parse_pip_audit_json(PIP_AUDIT_JSON)
    unaccepted, stale = audit.reconcile(found, entries)
    assert unaccepted == [], (
        "the seeded allowlist should accept the known pydantic-settings + "
        f"cryptography closure advisories; unaccepted={unaccepted}"
    )
    assert stale == [], (
        "seeded allowlist entries must correspond to advisories actually "
        f"present in the sample closure; stale={stale}"
    )


def test_committed_allowlist_seeds_pydantic_settings() -> None:
    entries = audit.load_allowlist(ALLOWLIST.read_text())
    ids = {i.upper() for e in entries for i in e.all_ids}
    assert "GHSA-4XGF-CPJX-PC3J" in ids, (
        "the pydantic-settings advisory must be seeded per the finding"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
