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
3. Runs ``pip-audit`` (the SAME tool as the uv.lock job) against that pinned
   set with ``--no-deps`` — so it audits precisely what deploys, not a fresh
   PyPI re-resolution.
4. Reconciles the advisories against a checked-in allowlist
   (``closure-advisory-allowlist.toml``): each accepted advisory carries a
   one-line rationale (mirrors the pentest ``accepted_ledger``).

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

Determinism: ``pip-audit`` is pinned to an exact version (``PIP_AUDIT_SPEC``).
The vulnerability DB is live — same as the existing uv.lock job — which is
intended for a security gate: a newly disclosed advisory in the shipping
closure SHOULD turn the build red.

Run locally:  ``python3 nix/audit/closure_advisory_audit.py``
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ── configuration ─────────────────────────────────────────────────────

# Pinned so the scanner is reproducible; bump deliberately.
PIP_AUDIT_SPEC = "pip-audit==2.10.1"

# Production packages whose runtime closure IS what deploys. The router
# wrapper's closure is a superset of the backend's python tree, but both
# are cheap to name and keep the intent explicit.
CLOSURE_TARGETS = [".#agent-mcp", ".#agent-mcp-router-wrapper"]

_AUDIT_DIR = Path(__file__).resolve().parent
DEFAULT_ALLOWLIST = _AUDIT_DIR / "closure-advisory-allowlist.toml"

# `/nix/store/<hash>-python3.<minor>-<pname>-<version>`. The dotted
# `python3.N-` prefix is what distinguishes a python package from the
# interpreter derivation (`python3-3.14.6`) and from C libraries.
_STORE_PY = re.compile(
    r"-python3\.\d+-(?P<name>.+?)-(?P<version>\d[^-]*(?:\.[^-]*)*)$"
)


def canonicalize(name: str) -> str:
    """PEP 503 name normalization so store/uv.lock/pip-audit names line up."""
    return re.sub(r"[-_.]+", "-", name).lower()


# ── closure -> python versions ────────────────────────────────────────


def parse_python_packages(requisites: Iterable[str]) -> dict[str, str]:
    """Map canonical package name -> version from ``nix-store -qR`` lines.

    Non-python store paths (the interpreter, glibc, …) do not carry the
    dotted ``python3.N-`` prefix and are skipped. If two closure entries
    disagree on a package version (should not happen for a single runtime
    closure) the lower version wins — the conservative choice for an audit.
    """
    out: dict[str, str] = {}
    for line in requisites:
        m = _STORE_PY.search(line.strip())
        if not m:
            continue
        name = canonicalize(m.group("name"))
        version = m.group("version")
        prev = out.get(name)
        if prev is None or _lower_version(version, prev):
            out[name] = version
    return out


def _lower_version(a: str, b: str) -> bool:
    """True if ``a`` sorts below ``b`` (best-effort numeric tuple compare)."""

    def parts(v: str) -> list:
        return [int(p) if p.isdigit() else p for p in re.split(r"[.\-+]", v)]

    try:
        return parts(a) < parts(b)
    except TypeError:
        return a < b


def requirements_text(packages: dict[str, str]) -> str:
    """Render a fully-pinned requirements file for ``pip-audit --no-deps``."""
    return "".join(f"{name}=={ver}\n" for name, ver in sorted(packages.items()))


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


def parse_pip_audit_json(data: dict) -> list[Advisory]:
    """Extract advisories from ``pip-audit --format json`` output."""
    advisories: list[Advisory] = []
    for dep in data.get("dependencies", []):
        for vuln in dep.get("vulns", []) or []:
            ids = {vuln["id"], *(vuln.get("aliases") or [])}
            advisories.append(
                Advisory(
                    package=canonicalize(dep["name"]),
                    version=dep.get("version", ""),
                    ids=frozenset(ids),
                    fix_versions=tuple(vuln.get("fix_versions") or []),
                )
            )
    return advisories


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


def _nix_env() -> dict:
    """Ensure the flakes/nix-command features are on without clobbering any
    NIX_CONFIG the caller (CI, a dev shell) already set."""
    import os

    existing = os.environ.get("NIX_CONFIG", "")
    feature_line = "experimental-features = nix-command flakes"
    nix_config = existing if "experimental-features" in existing else (
        f"{existing}\n{feature_line}".strip()
    )
    return {**os.environ, "NIX_CONFIG": nix_config}


def run_pip_audit(requirements: str) -> dict:
    """Run pinned pip-audit over the pinned requirements, return parsed JSON."""
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as fh:
        fh.write(requirements)
        req_path = fh.name
    try:
        proc = _run(
            [
                "uvx",
                "--from",
                PIP_AUDIT_SPEC,
                "pip-audit",
                "--no-deps",
                "--progress-spinner",
                "off",
                "--format",
                "json",
                "-r",
                req_path,
            ],
            env={**os.environ},
        )
    finally:
        os.unlink(req_path)
    # pip-audit exits non-zero when advisories are found; that is expected —
    # we parse JSON regardless and decide via the allowlist. Only a genuine
    # tool error (no JSON on stdout) is fatal.
    stdout = proc.stdout.strip()
    if not stdout:
        raise SystemExit(
            "pip-audit produced no JSON output; it likely errored:\n"
            f"{proc.stderr}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"could not parse pip-audit JSON ({exc}):\n{stdout}\n{proc.stderr}"
        )


# ── CLI ───────────────────────────────────────────────────────────────


def _format_advisory(adv: Advisory) -> str:
    fix = f" (fixed in {', '.join(adv.fix_versions)})" if adv.fix_versions else ""
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
        help="skip the nix build; audit this pinned requirements file",
    )
    ap.add_argument(
        "--audit-json",
        type=Path,
        help="skip pip-audit; reconcile this pip-audit JSON file",
    )
    args = ap.parse_args(argv)

    allowlist = load_allowlist(args.allowlist.read_text())

    if args.audit_json:
        audit_data = json.loads(args.audit_json.read_text())
    else:
        if args.requirements:
            reqs_text = args.requirements.read_text()
        else:
            print("Building deploy closure:", " ".join(CLOSURE_TARGETS))
            requisites = build_closure_requisites(CLOSURE_TARGETS)
            packages = parse_python_packages(requisites)
            print(f"Closure ships {len(packages)} python packages.")
            reqs_text = requirements_text(packages)
        print("Running", PIP_AUDIT_SPEC, "against the closure...")
        audit_data = run_pip_audit(reqs_text)

    found = parse_pip_audit_json(audit_data)
    unaccepted, stale = reconcile(found, allowlist)

    print()
    print(f"Advisories in shipping closure: {len(found)}")
    for adv in found:
        state = "UNACCEPTED" if adv in unaccepted else "accepted"
        print(f"  [{state}] {_format_advisory(adv)}")

    ok = True
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
