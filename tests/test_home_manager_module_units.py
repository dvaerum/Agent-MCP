"""Regression guard: the home-manager module's per-project backend
template (``agent-mcp@<name>.service``) must generate the
``forwarding_hmac`` key file in an ExecStartPre, the same way the NixOS
module does (PRs #214, #216, #217).

Background
----------

PRs #214/#216/#217 ported the per-project HMAC key generation from the
router (Python) into the systemd unit's ExecStartPre. The rationale,
quoted from PR #214:

    Old: router generates key, writes to disk, invokes systemctl start.
         Self-heal on cache hit covers the router-driven restart, but
         systemd's ``Restart=on-failure`` runs without the router.

    New: systemd unit's ExecStartPre owns key generation. EVERY path
         that starts the unit — manual ``systemctl start``, on-failure
         restart, boot-time activation — guarantees the file is on disk
         before the backend's ``--forwarding-hmac-in`` validator runs.

That fix landed in ``nix/module.nix`` (system-mode NixOS module). The
home-manager template (``nix/home-manager-module.nix``) was never
updated to match — the same drift pattern as PR #223 (the router-DB
env-var port). Deployed home-manager systems hit:

    agent-mcp-launcher: Error: Invalid value for '--forwarding-hmac-in':
      File '/run/user/1000/agent-mcp/<name>/forwarding_hmac' does not exist.
    systemd: agent-mcp@<name>.service: Main process exited,
      code=exited, status=2/INVALIDARGUMENT
    systemd: Scheduled restart job, restart counter is at 630.

These tests pin the home-manager template's ExecStartPre to the same
shape as the NixOS module, adapted for user-scope:

- The ExecStartPre block must include a script that creates
  ``forwarding_hmac`` from ``/dev/urandom`` if missing.
- The script must use ``pkgs.runtimeShell`` (PR #216 fix: coreutils
  does NOT ship ``sh``, the unit fails 203/EXEC otherwise).
- The script must write exactly 32 raw bytes (PR #217 fix: don't
  ``.strip()``, the bytes are binary and any whitespace at the
  boundary is data).
- The file mode must be 0600.
- The unit must declare ``RuntimeDirectory=agent-mcp/%i`` so the
  parent dir exists with the right owner/mode.
- The existing socket-removal ExecStartPre must still be present (the
  pre-existing defensive cleanup).
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_HM_MODULE = _REPO_ROOT / "nix" / "home-manager-module.nix"


def _extract_backend_template_block(text: str) -> str:
    """Return the raw nix source of the ``"agent-mcp@"`` systemd user
    service block, from its opening brace to the close of the Service
    attrset.

    Anchors:
      - opening marker: ``"agent-mcp@" = {``
      - closing marker: the next ``"agent-mcp-router" = {`` (next entry
        in the systemd.user.services attrset).
    """
    open_marker = '"agent-mcp@" = {'
    close_marker = '"agent-mcp-router" = {'
    start = text.index(open_marker)
    end = text.index(close_marker, start)
    return text[start:end]


def test_backend_template_declares_runtime_directory() -> None:
    """The per-project backend unit must declare
    ``RuntimeDirectory=agent-mcp/%i`` so systemd creates
    ``$XDG_RUNTIME_DIR/agent-mcp/<name>/`` with the right owner/mode
    before any ExecStartPre runs."""
    text = _HM_MODULE.read_text()
    block = _extract_backend_template_block(text)
    assert re.search(r'RuntimeDirectory\s*=\s*"agent-mcp/%i"', block), (
        'home-manager-module.nix: "agent-mcp@" service must set '
        'RuntimeDirectory = "agent-mcp/%i" so the parent dir exists '
        "before ExecStartPre tries to write forwarding_hmac into it."
    )


def test_backend_template_generates_forwarding_hmac() -> None:
    """The per-project backend unit's ExecStartPre must include a
    script that creates ``forwarding_hmac`` from /dev/urandom when
    missing.

    The NixOS module ships exactly this; the home-manager template
    must mirror it. The drift caused real deploys to crash-loop with
    ``--forwarding-hmac-in`` pointing at a non-existent file."""
    text = _HM_MODULE.read_text()
    block = _extract_backend_template_block(text)
    # Must reference forwarding_hmac as a filename in an ExecStartPre
    # entry. The path uses systemd's %t (= $XDG_RUNTIME_DIR) or the
    # $RUNTIME_DIRECTORY env var that systemd sets when
    # RuntimeDirectory= is configured.
    assert "forwarding_hmac" in block, (
        'home-manager-module.nix: "agent-mcp@" service must contain an '
        "ExecStartPre that generates the forwarding_hmac key file. The "
        "NixOS module gained this in PR #214 (F015 v4); the home-manager "
        "template was never updated, causing backend crash-loop with "
        "Error: Invalid value for '--forwarding-hmac-in': File '...' "
        "does not exist."
    )
    # The generator must read from /dev/urandom (head -c 32). Match the
    # head invocation that produces 32 bytes.
    assert re.search(r"head\s+-c\s+32\s+/dev/urandom", block), (
        "home-manager-module.nix: forwarding_hmac generator must use "
        "`head -c 32 /dev/urandom` (32 raw bytes). The NixOS module's "
        "pattern is the reference; bytes are binary and must NOT be "
        "stripped or transformed (see PR #217)."
    )
    # The mode must be 0600 — set via chmod after creation.
    assert re.search(r"chmod\s+600", block), (
        "home-manager-module.nix: forwarding_hmac must be chmod 600 "
        "after creation; the key is sensitive material."
    )


def test_backend_template_uses_runtime_shell_for_execstartpre() -> None:
    """The ExecStartPre script that generates forwarding_hmac must use
    ``pkgs.runtimeShell``, not ``${pkgs.coreutils}/bin/sh``.

    PR #216 (F015 v6): coreutils does NOT ship ``sh``. The original
    F015 v4 used ``${pkgs.coreutils}/bin/sh`` and the unit failed every
    start with ``status=203/EXEC``. The fix is ``pkgs.runtimeShell``."""
    text = _HM_MODULE.read_text()
    block = _extract_backend_template_block(text)
    # The shell that wraps the forwarding_hmac generator must be
    # runtimeShell. Match the substring on the same line as the
    # forwarding_hmac literal.
    hmac_lines = [
        line for line in block.splitlines() if "forwarding_hmac" in line
    ]
    assert hmac_lines, "forwarding_hmac line not found (other test catches this)"
    assert any("pkgs.runtimeShell" in line for line in hmac_lines), (
        "home-manager-module.nix: the ExecStartPre that generates "
        "forwarding_hmac must invoke `${pkgs.runtimeShell}`. Using "
        "`${pkgs.coreutils}/bin/sh` fails with 203/EXEC because coreutils "
        "does not ship sh (PR #216 / F015 v6)."
    )
    # Defense in depth: the bad pattern from F015 v4 must NOT appear
    # in any non-comment line.
    code_lines = [
        line
        for line in block.splitlines()
        if "${pkgs.coreutils}/bin/sh" in line
        and not line.lstrip().startswith("#")
    ]
    assert code_lines == [], (
        "home-manager-module.nix: ${pkgs.coreutils}/bin/sh in the "
        '"agent-mcp@" service breaks every start with status=203/EXEC. '
        "Use ${pkgs.runtimeShell} instead (PR #216 / F015 v6). "
        f"Bad lines: {code_lines!r}"
    )


def test_backend_template_generator_is_idempotent() -> None:
    """The forwarding_hmac generator must be a no-op if the file
    already exists (the router caches the bytes in memory; rotating
    the key on every restart would break the cache invariant — see
    commit 862e594).

    The pattern is ``test -f <path> || { generate; }`` — only create
    when missing."""
    text = _HM_MODULE.read_text()
    block = _extract_backend_template_block(text)
    # Look for the `test -f ... forwarding_hmac` guard.
    assert re.search(
        r"test\s+-f\s+[^|]*forwarding_hmac", block
    ), (
        "home-manager-module.nix: forwarding_hmac generator must be "
        "idempotent — guard with `test -f <path>/forwarding_hmac || "
        "{ generate; }`. Without the guard, every restart rotates the "
        "key and invalidates the router's in-memory cache."
    )


def test_backend_template_keeps_socket_cleanup() -> None:
    """The pre-existing ExecStartPre that removes a stale backend.sock
    must still be present alongside the new forwarding_hmac generator.

    Adding the HMAC generator must not regress the socket cleanup that
    existed before this fix."""
    text = _HM_MODULE.read_text()
    block = _extract_backend_template_block(text)
    assert re.search(r"rm\s+-f[^\"]*backend\.sock", block), (
        'home-manager-module.nix: "agent-mcp@" service must still '
        "remove a stale backend.sock in ExecStartPre. The HMAC-generator "
        "addition must not regress the existing socket cleanup."
    )
