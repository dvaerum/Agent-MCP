#!/usr/bin/env python3
"""Advisory audit for the ACTUAL nix deploy closure (R13-F1).

Why this exists
---------------

The ``dependency-audit`` CI job runs ``pip-audit`` against ``uv.lock`` — the
dev / ``pip install`` resolution universe. But the DEPLOYED artifact is built
by ``nix/packages.nix`` (``buildPythonApplication``) entirely from the pinned
nixpkgs channel and never reads ``uv.lock`` (``grep uv.lock nix/`` == nothing).
Two independent resolution universes. A version the uv.lock audit marks clean
can ship vulnerable in the nix closure, and vice-versa — nothing audited the
shipping closure until this gate.

Proven divergence on the current pin (2026-08-09): the closure ships
``pydantic-settings 2.12.0`` (GHSA-4xgf-cpjx-pc3j) and ``cryptography 49.0.0``
(CVE-2026-69247) while ``uv.lock`` pins versions past both fixes.

What it does
------------

1. Builds the production packages (``.#agent-mcp`` and the router wrapper) and
   walks their runtime closure (``nix-store -qR``).
2. Extracts the exact python package versions the closure ships.
3. Queries the OSV database for advisories affecting each shipped
   *name==version* coordinate.
4. Reconciles the advisories against a checked-in allowlist
   (``closure-advisory-allowlist.toml``): each accepted advisory carries a
   one-line rationale (mirrors the pentest ``accepted_ledger``).

Why OSV, not ``pip-audit -r`` on the closure
--------------------------------------------

``pip-audit``'s requirements path shells out to ``pip install --dry-run`` to
resolve the tree, so it can only audit versions that are INSTALLABLE from
PyPI. A nix closure legitimately carries versions PyPI never published (e.g.
this closure ships ``joserfc 1.6.9``, which does not exist on PyPI — the
releases jump 1.6.8 -> 1.7.0). An install-based auditor hard-fails on the
first such coordinate, so it is the wrong tool for a nix closure. OSV's query
API audits by *coordinate* (name + version), never installs, and is the same
upstream vulnerability data ``pip-audit`` itself consumes — so the closure
gets audited against the same advisories as the uv.lock leg, without the
install limitation.

The gate is actionable, not a firehose: it fails only on a NEW *advisory* in
the shipping closure — not on every version that merely lags PyPI. (An
advisory-scoped audit was chosen over a raw closure vuln-scan like vulnix,
which flags dozens of unfixable transitive nixpkgs CVEs, and over a plain
"closure < uv.lock pin" drift detector, which on the current pin flags 8
packages where only 2 carry advisories — 6 benign-lag entries that would rot
the allowlist. See ``.github/workflows/ci.yml`` and the finding writeup.)

Both directions are enforced:

* an advisory found in the closure but NOT allowlisted -> FAIL (the point of
  the gate);
* an allowlist entry that matches NO current advisory -> FAIL as stale, so an
  ignore cannot outlive the advisory it excused ("an ignore needs a reason AND
  the follow-up that removes it").

Determinism: the scanner is this checked-in script talking to the pinned OSV
API endpoint. The vulnerability DB is live — same as the existing uv.lock job
— which is intended for a security gate: a newly disclosed advisory in the
shipping closure SHOULD turn the build red.

Fail loud, never silent (R14-F3)
--------------------------------

The gate's own failure mode is a silent skip: a python store path the parser
cannot turn into a real ``name==version`` coordinate is queried as garbage (or
not at all), OSV returns nothing, and the package passes without ever being
audited — invisible. That is the exact silent-control class this whole gate
exists to eliminate. So:

* version parsing strips nixpkgs' build-provenance suffixes
  (``-unstable-YYYY-MM-DD``, ``-git-<hash>``, ``-rcN``, ``-pre*``, date
  snapshots, …) to recover the base version — the suffixed pin is precisely the
  unreleased case most likely to lag a security fix, so it MUST be audited;
* any ``python3.N-``-prefixed path that STILL does not yield a usable
  coordinate (e.g. the ``0.0.0-unknown`` pyproject fallback) is reported as
  ``unparseable`` and FAILS the gate — a package that cannot be audited is made
  visible, not dropped. The app's own derivations (``APP_PACKAGES``) are the
  one exclusion, by name — they are not OSV-tracked deps.

Known residual — name divergence
--------------------------------

OSV is queried by the nix pname (PEP 503-canonicalized). If a nix pname differs
from the package's OSV/PyPI name, the query returns empty and the coordinate
passes silently — a real, harder-to-close silent-miss (there is no
authoritative nix-pname -> PyPI-name table). Nixpkgs keeps python pnames
aligned with PyPI as policy, so this is rare; ``_PYPI_NAME_OVERRIDES`` gives
any divergence that surfaces a home. This residual is documented, not fully
closed.

Run locally:  ``python3 nix/audit/closure_advisory_audit.py``
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ── configuration ─────────────────────────────────────────────────────

OSV_API = "https://api.osv.dev"
OSV_TIMEOUT = 120  # seconds per HTTP call

# Production packages whose runtime closure IS what deploys. The router
# wrapper's closure is a superset of the backend's python tree, but both
# are cheap to name and keep the intent explicit.
CLOSURE_TARGETS = [".#agent-mcp", ".#agent-mcp-router-wrapper"]

_AUDIT_DIR = Path(__file__).resolve().parent
DEFAULT_ALLOWLIST = _AUDIT_DIR / "closure-advisory-allowlist.toml"

# `/nix/store/<hash>-python3.<minor>-<pname>-<version>[<suffix>]`. The dotted
# `python3.N-` prefix is what distinguishes a python package from the
# interpreter derivation (`python3-3.14.6`) and from C libraries. `rest` is
# `<pname>-<version>[<suffix>]`; it is split further below. The prefix match is
# deliberately NOT version-shaped: a python store path must be RECOGNISED as
# python first, so an unparseable tail can be failed loud rather than skipped
# silently (R14-F3 — the old regex forbade a hyphen inside the version group,
# so an `-unstable-*` / `-rcN` / `0.0.0-unknown` tail simply did not match and
# the package fell through unaudited AND unflagged: a silent pass).
_STORE_PY = re.compile(r"-python3\.\d+-(?P<rest>.+)$")

# Within `rest`, the version starts at the first hyphen-delimited component
# beginning with a digit — nix's own `parseDrvName` rule. This lets a
# hyphenated pname (`argon2-cffi-bindings`) split cleanly from its version.
_NAME_VERSION = re.compile(r"^(?P<name>.+?)-(?P<version>\d.*)$")

# nixpkgs decorates a base version with build-provenance suffixes for
# unreleased pins: `-unstable-YYYY-MM-DD`, `-git-<hash>`, `-pre*`, `-rcN`,
# `-alpha*`/`-beta*`/`-dev*`/`-post*`, a bare `-YYYY-MM-DD` date snapshot, etc.
# Strip them to recover the base version the OSV query needs. The suffixed pin
# is exactly the unreleased case most likely to LAG a security fix, so it must
# be audited against its real version, never skipped.
_VERSION_SUFFIX = re.compile(
    r"-(?:unstable|git|pre|post|dev|rc|alpha|beta|snapshot|nightly"
    r"|\d{4}-\d{2}-\d{2}).*$",
    re.IGNORECASE,
)

# A usable OSV coordinate needs a real dotted-numeric base version. The
# pyproject fallback `0.0.0-unknown` (buildPythonApplication when it cannot
# read a version) leaves a non-numeric `-unknown` residue that no suffix rule
# strips — so it does NOT fullmatch here and is surfaced as unparseable rather
# than queried as garbage.
_CLEAN_VERSION = re.compile(r"\d+(?:\.\d+)*")

# The app's OWN derivations are not OSV-tracked dependencies. A `0.0.0-unknown`
# fallback (or any odd version) on one of these must be excluded BY NAME — not
# by silently dropping unparseables broadly, and not by failing the gate on the
# app auditing itself.
APP_PACKAGES = frozenset(
    {"agent-mcp", "agent-mcp-router-wrapper", "agent-mcp-dashboard"}
)

# KNOWN RESIDUAL (documented, partially mitigated): OSV is queried by the nix
# pname (PEP 503-canonicalized). When a nix pname differs from the package's
# OSV/PyPI name, the query returns empty and the coordinate passes SILENTLY —
# the same silent-miss class as the parse bug, but harder to close in general
# (there is no authoritative nix-pname -> PyPI-name table). Nixpkgs keeps
# python pnames aligned with PyPI names as a policy, so divergences are rare;
# the mechanism below gives any that surface a home. Add `"<nix-pname>":
# "<pypi-name>"` here (both canonicalized) when the audit or a CVE writeup
# shows a package whose nix pname is not its PyPI/OSV name.
_PYPI_NAME_OVERRIDES: dict[str, str] = {}


def osv_query_name(name: str) -> str:
    """Map a canonical nix pname to the name OSV/PyPI knows it by. Identity for
    all but the (rare) documented divergences in ``_PYPI_NAME_OVERRIDES``."""
    return _PYPI_NAME_OVERRIDES.get(name, name)


def canonicalize(name: str) -> str:
    """PEP 503 name normalization so store/uv.lock/OSV names line up."""
    return re.sub(r"[-_.]+", "-", name).lower()


# ── closure -> python versions ────────────────────────────────────────


@dataclass(frozen=True)
class ClosureScan:
    """Classification of ``nix-store -qR`` output for the audit.

    ``packages`` maps canonical pname -> base version for every python package
    that yielded a usable OSV coordinate. ``unparseable`` lists python store
    paths that carry the ``python3.N-`` prefix but did NOT yield a usable
    coordinate and are not one of the app's own derivations. The gate FAILS
    LOUD on ``unparseable`` (R14-F3): a package that cannot be audited must be
    VISIBLE, never silently skipped — the exact silent-control class this gate
    exists to eliminate.
    """

    packages: dict[str, str]
    unparseable: list[str]


def _base_version(version_full: str) -> str | None:
    """Recover the dotted-numeric base version from a nixpkgs version string,
    stripping build-provenance suffixes (``-unstable-*``, ``-git-*``, ``-rcN``,
    ``-pre*``, date snapshots, …). Returns ``None`` when what remains is not a
    real version (e.g. the ``0.0.0-unknown`` fallback, whose ``-unknown`` tail
    is not a recognised suffix and leaves a non-numeric residue)."""
    stripped = _VERSION_SUFFIX.sub("", version_full)
    return stripped if _CLEAN_VERSION.fullmatch(stripped) else None


def scan_closure(requisites: Iterable[str]) -> ClosureScan:
    """Split ``nix-store -qR`` output into auditable coordinates and the
    unparseable python paths the gate must fail loud on.

    Non-python store paths (the interpreter, glibc, …) do not carry the dotted
    ``python3.N-`` prefix and are ignored. The app's own derivations are
    excluded by name (not OSV-tracked deps). If two closure entries disagree on
    a package version (should not happen for a single runtime closure) the lower
    version wins — the conservative choice for an audit.
    """
    packages: dict[str, str] = {}
    unparseable: list[str] = []
    for line in requisites:
        path = line.strip()
        m = _STORE_PY.search(path)
        if not m:
            continue  # not a python package
        nv = _NAME_VERSION.match(m.group("rest"))
        name = canonicalize(nv.group("name")) if nv else ""
        if name in APP_PACKAGES:
            continue  # the app's own derivation — not an OSV-tracked dep
        base = _base_version(nv.group("version")) if nv else None
        if not name or base is None:
            # python-prefixed but no usable coordinate — fail loud, don't skip.
            unparseable.append(path)
            continue
        prev = packages.get(name)
        if prev is None or _lower_version(base, prev):
            packages[name] = base
    return ClosureScan(packages=packages, unparseable=unparseable)


def parse_python_packages(requisites: Iterable[str]) -> dict[str, str]:
    """Coordinate-only view of :func:`scan_closure`: canonical pname -> base
    version for the auditable python packages. Callers that also need the
    fail-loud data use :func:`scan_closure` directly."""
    return scan_closure(requisites).packages


def _lower_version(a: str, b: str) -> bool:
    """True if ``a`` sorts below ``b`` (best-effort numeric tuple compare)."""

    def parts(v: str) -> list:
        return [int(p) if p.isdigit() else p for p in re.split(r"[.\-+]", v)]

    try:
        return parts(a) < parts(b)
    except TypeError:
        return a < b


# ── advisories ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Advisory:
    package: str  # canonicalized
    version: str
    ids: frozenset[str]  # primary id + aliases, verbatim casing
    fix_versions: tuple[str, ...] = ()

    @property
    def display_id(self) -> str:
        return min(self.ids) if self.ids else "?"


def _osv_fixed_versions(detail: dict, package: str) -> tuple[str, ...]:
    """Best-effort: pull the ``fixed`` events for ``package`` from an OSV
    record's ``affected`` ranges (informational, for the report)."""
    fixed: list[str] = []
    for affected in detail.get("affected", []) or []:
        pkg = (affected.get("package") or {}).get("name", "")
        if canonicalize(pkg) != canonicalize(package):
            continue
        for rng in affected.get("ranges", []) or []:
            for event in rng.get("events", []) or []:
                if "fixed" in event:
                    fixed.append(event["fixed"])
    return tuple(dict.fromkeys(fixed))


def advisories_from_osv(
    coords: list[tuple[str, str]],
    batch_results: list[dict],
    details_by_id: dict[str, dict],
) -> list[Advisory]:
    """Build advisories from an OSV ``querybatch`` response.

    ``coords`` is the queried ``(name, version)`` list in the same order as
    ``batch_results`` (OSV preserves query order). ``details_by_id`` maps a
    vuln id to its full OSV record (``GET /v1/vulns/{id}``), which is where
    the aliases live — the batch response carries ids only.
    """
    advisories: list[Advisory] = []
    for (name, version), result in zip(coords, batch_results):
        for vuln in result.get("vulns", []) or []:
            vid = vuln["id"]
            detail = details_by_id.get(vid, {})
            ids = {vid, *(detail.get("aliases") or [])}
            advisories.append(
                Advisory(
                    package=canonicalize(name),
                    version=version,
                    ids=frozenset(ids),
                    fix_versions=_osv_fixed_versions(detail, name),
                )
            )
    return _merge_aliased(advisories)


def _merge_aliased(advisories: list[Advisory]) -> list[Advisory]:
    """Collapse records that are the SAME advisory under different ids.

    OSV returns aliases (GHSA / PYSEC / CVE) of one advisory as separate
    records, so a single vulnerability can appear two or three times for one
    package. Merge records for the same package whose id sets overlap so the
    report shows each real advisory once. Two genuinely distinct advisories on
    the same package have disjoint id sets and stay separate.
    """
    merged: list[Advisory] = []
    for adv in advisories:
        for i, existing in enumerate(merged):
            if existing.package == adv.package and (existing.ids & adv.ids):
                merged[i] = Advisory(
                    package=existing.package,
                    version=existing.version or adv.version,
                    ids=existing.ids | adv.ids,
                    fix_versions=tuple(
                        dict.fromkeys(existing.fix_versions + adv.fix_versions)
                    ),
                )
                break
        else:
            merged.append(adv)
    return merged


# ── allowlist ─────────────────────────────────────────────────────────


@dataclass
class AllowEntry:
    id: str
    package: str
    rationale: str
    aliases: tuple[str, ...] = ()
    fixed_in: str = ""

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset({self.id, *self.aliases})


def load_allowlist(text: str) -> list[AllowEntry]:
    data = tomllib.loads(text)
    entries: list[AllowEntry] = []
    for raw in data.get("accepted", []):
        entries.append(
            AllowEntry(
                id=raw["id"],
                package=raw["package"],
                rationale=raw["rationale"],
                aliases=tuple(raw.get("aliases", [])),
                fixed_in=raw.get("fixed_in", ""),
            )
        )
    return entries


def _matches(entry: AllowEntry, adv: Advisory) -> bool:
    if canonicalize(entry.package) != adv.package:
        return False
    entry_ids = {i.upper() for i in entry.all_ids}
    adv_ids = {i.upper() for i in adv.ids}
    return bool(entry_ids & adv_ids)


def reconcile(
    found: list[Advisory], allowlist: list[AllowEntry]
) -> tuple[list[Advisory], list[AllowEntry]]:
    """Return ``(unaccepted_advisories, stale_allowlist_entries)``."""
    unaccepted = [
        adv for adv in found if not any(_matches(e, adv) for e in allowlist)
    ]
    stale = [
        e for e in allowlist if not any(_matches(e, adv) for adv in found)
    ]
    return unaccepted, stale


# ── orchestration (IO) ────────────────────────────────────────────────


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kw)


def _nix_env() -> dict:
    """Ensure the flakes/nix-command features are on without clobbering any
    NIX_CONFIG the caller (CI, a dev shell) already set."""
    import os

    existing = os.environ.get("NIX_CONFIG", "")
    feature_line = "experimental-features = nix-command flakes"
    nix_config = (
        existing
        if "experimental-features" in existing
        else f"{existing}\n{feature_line}".strip()
    )
    return {**os.environ, "NIX_CONFIG": nix_config}


def build_closure_requisites(targets: list[str]) -> list[str]:
    """``nix build`` the targets and return every runtime requisite path."""
    build = _run(
        [
            "nix",
            "build",
            *targets,
            "--no-link",
            "--print-out-paths",
            # Build from source if a binary-cache substitution fails, rather
            # than hard-failing on a flaky cache.nixos.org (mirrors the VM job).
            "--fallback",
        ],
        env=_nix_env(),
    )
    if build.returncode != 0:
        raise SystemExit(f"nix build failed:\n{build.stderr}")
    out_paths = build.stdout.split()
    reqs = _run(["nix-store", "-qR", *out_paths], env=_nix_env())
    if reqs.returncode != 0:
        raise SystemExit(f"nix-store -qR failed:\n{reqs.stderr}")
    return reqs.stdout.splitlines()


def _osv_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{OSV_API}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=OSV_TIMEOUT) as resp:
        return json.loads(resp.read())


def _osv_get(path: str) -> dict:
    req = urllib.request.Request(f"{OSV_API}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=OSV_TIMEOUT) as resp:
        return json.loads(resp.read())


def query_osv(packages: dict[str, str]) -> list[Advisory]:
    """Audit every ``name==version`` coordinate against OSV."""
    coords = sorted(packages.items())
    # Query OSV by the PyPI/OSV name (see `_PYPI_NAME_OVERRIDES`) but keep the
    # canonical nix pname in `coords` so advisories and allowlist entries match
    # on the same name the closure ships.
    queries = [
        {
            "package": {"ecosystem": "PyPI", "name": osv_query_name(name)},
            "version": version,
        }
        for name, version in coords
    ]
    batch = _osv_post("/v1/querybatch", {"queries": queries})
    results = batch.get("results", [])
    # Fetch full records (for aliases) only for the ids that actually hit.
    hit_ids = {
        v["id"]
        for result in results
        for v in (result.get("vulns") or [])
    }
    details_by_id = {vid: _osv_get(f"/v1/vulns/{vid}") for vid in sorted(hit_ids)}
    return advisories_from_osv(coords, results, details_by_id)


# ── CLI ───────────────────────────────────────────────────────────────


def _format_advisory(adv: Advisory) -> str:
    fix = (
        f" (fixed in {', '.join(adv.fix_versions)})" if adv.fix_versions else ""
    )
    return f"{adv.package} {adv.version}: {', '.join(sorted(adv.ids))}{fix}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help="path to the advisory allowlist TOML",
    )
    ap.add_argument(
        "--requirements",
        type=Path,
        help="skip the nix build; audit this pinned requirements file "
        "(name==version per line)",
    )
    ap.add_argument(
        "--osv-json",
        type=Path,
        help="skip OSV; reconcile this pre-fetched advisories JSON "
        "(list of {package, version, ids, fix_versions})",
    )
    args = ap.parse_args(argv)

    allowlist = load_allowlist(args.allowlist.read_text())

    # Python store paths the parser could not turn into an OSV coordinate.
    # Only the real-closure path can produce these; they FAIL the gate loud
    # (R14-F3) rather than being silently skipped.
    unparseable: list[str] = []

    if args.osv_json:
        raw = json.loads(args.osv_json.read_text())
        found = [
            Advisory(
                package=canonicalize(r["package"]),
                version=r.get("version", ""),
                ids=frozenset(r.get("ids", [])),
                fix_versions=tuple(r.get("fix_versions", [])),
            )
            for r in raw
        ]
    else:
        if args.requirements:
            packages = {}
            for line in args.requirements.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                name, _, version = line.partition("==")
                packages[canonicalize(name)] = version
        else:
            print("Building deploy closure:", " ".join(CLOSURE_TARGETS))
            requisites = build_closure_requisites(CLOSURE_TARGETS)
            scan = scan_closure(requisites)
            packages = scan.packages
            unparseable = scan.unparseable
        print(f"Closure ships {len(packages)} python packages.")
        print("Querying OSV for advisories affecting the shipped versions...")
        found = query_osv(packages)

    unaccepted, stale = reconcile(found, allowlist)

    print()
    print(f"Advisories in shipping closure: {len(found)}")
    for adv in found:
        state = "UNACCEPTED" if adv in unaccepted else "accepted"
        print(f"  [{state}] {_format_advisory(adv)}")

    ok = True
    if unparseable:
        ok = False
        print()
        print("FAIL: python store paths that did NOT yield an auditable OSV")
        print("  coordinate — they were NOT queried against OSV. A package that")
        print("  cannot be audited must be VISIBLE, not silently skipped")
        print("  (R14-F3). Teach the parser the version suffix in")
        print("  nix/audit/closure_advisory_audit.py (_VERSION_SUFFIX), or — if")
        print("  this is one of the app's own derivations — add it to")
        print("  APP_PACKAGES:")
        for p in unparseable:
            print(f"    - {p}")

    if unaccepted:
        ok = False
        print()
        print("FAIL: new advisory in the deploy closure, not in the allowlist.")
        print("  Fix the real risk (bump the nixpkgs pin so the closure ships")
        print("  a fixed version), or — if genuinely unreachable/unfixable —")
        print(f"  add an entry to {args.allowlist} with a one-line rationale:")
        for adv in unaccepted:
            print(f"    - {_format_advisory(adv)}")

    if stale:
        ok = False
        print()
        print("FAIL: stale allowlist entries — no matching advisory in the")
        print("  closure anymore. Remove them (the risk they excused is gone):")
        for e in stale:
            print(f"    - {e.package}: {e.id}  ({e.rationale})")

    if ok:
        print()
        print("OK: every closure advisory is accounted for; no stale entries.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
