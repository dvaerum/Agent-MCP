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
ACTUAL nix-closure python package versions and queries OSV for advisories
affecting those exact coordinates, reconciled against a checked-in advisory
allowlist so the gate is actionable (red == a NEW unaccepted advisory in the
shipping closure, not "nixpkgs has CVEs"). These tests exercise the pure
parse / reconcile logic — no nix build, no network.
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


# ── OSV response parsing ──────────────────────────────────────────────

# Shape returned by OSV `POST /v1/querybatch` (ids only, query-order) plus the
# per-id detail from `GET /v1/vulns/{id}` (where aliases live). cryptography is
# reported by OSV as TWO aliased records (GHSA + PYSEC of the same CVE); the
# audit must handle that without choking. pydantic-settings has no CVE/PYSEC.
OSV_COORDS = [
    ("anyio", "4.14.2"),
    ("cryptography", "49.0.0"),
    ("pydantic-settings", "2.12.0"),
]
OSV_BATCH = [
    {"vulns": []},
    {"vulns": [{"id": "GHSA-g6cj-pr64-35w5"}, {"id": "PYSEC-2026-3552"}]},
    {"vulns": [{"id": "GHSA-4xgf-cpjx-pc3j"}]},
]
OSV_DETAILS = {
    "GHSA-g6cj-pr64-35w5": {
        "id": "GHSA-g6cj-pr64-35w5",
        "aliases": ["CVE-2026-69247", "PYSEC-2026-3552"],
        "affected": [
            {
                "package": {"ecosystem": "PyPI", "name": "cryptography"},
                "ranges": [
                    {"type": "ECOSYSTEM", "events": [{"introduced": "44.0.0"}, {"fixed": "50.0.0"}]}
                ],
            }
        ],
    },
    "PYSEC-2026-3552": {
        "id": "PYSEC-2026-3552",
        "aliases": ["CVE-2026-69247", "GHSA-g6cj-pr64-35w5"],
    },
    "GHSA-4xgf-cpjx-pc3j": {"id": "GHSA-4xgf-cpjx-pc3j", "aliases": []},
}


def _sample_found() -> list:
    return audit.advisories_from_osv(OSV_COORDS, OSV_BATCH, OSV_DETAILS)


def test_advisories_from_osv_enriches_ids_with_aliases() -> None:
    found = _sample_found()
    packages = {a.package for a in found}
    assert packages == {"cryptography", "pydantic-settings"}
    crypto_ids = {i for a in found if a.package == "cryptography" for i in a.ids}
    # Primary id and every alias are queryable for matching.
    assert "GHSA-g6cj-pr64-35w5" in crypto_ids
    assert "PYSEC-2026-3552" in crypto_ids
    assert "CVE-2026-69247" in crypto_ids


def test_advisories_from_osv_extracts_fixed_version() -> None:
    found = _sample_found()
    crypto = next(a for a in found if a.package == "cryptography" and a.fix_versions)
    assert "50.0.0" in crypto.fix_versions


# ── reconciliation ────────────────────────────────────────────────────


def test_empty_allowlist_flags_every_advisory() -> None:
    unaccepted, stale = audit.reconcile(_sample_found(), [])
    assert {a.package for a in unaccepted} == {"cryptography", "pydantic-settings"}
    assert stale == []


def test_allowlist_by_primary_id_accepts() -> None:
    allow = [
        audit.AllowEntry(
            id="GHSA-4xgf-cpjx-pc3j",
            package="pydantic-settings",
            aliases=(),
            rationale="unreachable",
        )
    ]
    unaccepted, stale = audit.reconcile(_sample_found(), allow)
    # cryptography still unaccepted; pydantic-settings cleared.
    assert {a.package for a in unaccepted} == {"cryptography"}
    assert stale == []


def test_allowlist_matches_by_alias() -> None:
    """An entry keyed on one alias clears an advisory OSV keyed on another."""
    allow = [
        audit.AllowEntry(
            id="CVE-2026-69247",  # neither the GHSA nor PYSEC primary id
            package="cryptography",
            aliases=(),
            rationale="unreachable",
        )
    ]
    unaccepted, _ = audit.reconcile(_sample_found(), allow)
    assert {a.package for a in unaccepted} == {"pydantic-settings"}


def test_allowlist_does_not_cross_packages() -> None:
    """A matching id under the wrong package name does not silence anything."""
    allow = [
        audit.AllowEntry(
            id="GHSA-4xgf-cpjx-pc3j",
            package="cryptography",  # wrong package for this id
            aliases=(),
            rationale="typo",
        )
    ]
    unaccepted, stale = audit.reconcile(_sample_found(), allow)
    assert {a.package for a in unaccepted} == {"cryptography", "pydantic-settings"}
    # The entry matched nothing -> stale.
    assert len(stale) == 1


def test_stale_allowlist_entry_is_flagged() -> None:
    """An accepted advisory no longer present in the closure must be removed."""
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
    unaccepted, stale = audit.reconcile(_sample_found(), allow)
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
    staying green (they are accepted with rationale). This uses the real OSV
    two-record shape for cryptography to prove aliased duplicates reconcile."""
    entries = audit.load_allowlist(ALLOWLIST.read_text())
    unaccepted, stale = audit.reconcile(_sample_found(), entries)
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
