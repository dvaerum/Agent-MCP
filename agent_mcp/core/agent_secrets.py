"""One owner for "which columns on an ``agents`` row are credentials".

N6 structural half (``docs/proposals/security-authz-architecture-hardening.md``,
Phase 5). Four surfaces withhold agent bearer secrets and all four agreed
on the *who* — :func:`agent_mcp.core.operator_tier.is_confirmed_operator_tier`,
already a single definition — but each answered *what is secret* and *what
to do about it* on its own:

===============================================  ==========================
site                                             mechanic
===============================================  ==========================
``tools/admin_tools.get_agent_tokens``           overwrite ``token`` with a
                                                 literal ``"***"``
``app/routers/composition./all-data``            ``pop`` ``token`` +
                                                 ``aoe_session_id``, then
                                                 re-add a gated
                                                 ``auth_token``
``app/routers/composition./node-details``        SQL column allowlist
``app/routers/settings./api/tokens``             403 or full plaintext
===============================================  ==========================

Those four *mechanics* are genuinely different and are deliberately NOT
collapsed into one — see "What is shared, and what isn't" below. What IS
collapsed is the vocabulary they were each restating: the secret column
set and the mask value, which is the half that could drift. It follows
``tools.project_settings_tools.redact_settings_row``'s idiom (a
``_SECRET_SETTING_KEYS`` declaration + a ``_REDACTED_VALUE`` + a
``redact_*`` function taking ``confirmed_operator_tier=``), applied to
agent rows instead of settings rows.

Where the secret set comes from
-------------------------------
The ORM model, via ``mapped_column(..., info={"secret": True})`` on
``db/models/agent.py``. Not a list in this module — a list here would be
the fifth hand-maintained copy of the same fact, and the one it replaced
(``composition._AGENT_NODE_SAFE_COLUMNS``) carried the comment "Keep this
in sync with the agents model when columns change", which is precisely
the class of invariant this plan exists to convert from discipline into
structure. A future credential column is declared secret where it is
declared, or it is secret nowhere.

What is shared, and what isn't
------------------------------
Shared: :data:`REDACTED_TOKEN`, :func:`agent_secret_columns`,
:func:`redact_agent_row`, :func:`strip_agent_secrets`.

NOT shared, on purpose, because unifying them would change what a given
caller tier receives:

* **mask vs. drop.** ``get_agent_tokens`` masks (the caller asked for the
  token column and gets a visible ``"***"`` placeholder); ``/all-data``
  drops (the raw column is an artefact of ``SELECT *`` — the endpoint's
  contract exposes a separate ``auth_token`` field, and adding a
  ``token: "***"`` key to that response would be a wire-shape change).
* **conditional vs. unconditional.** ``/node-details`` withholds the
  bearer from *every* tier including a confirmed operator — it is a
  display panel, not a credential surface. Routing it through a
  tier-conditional redactor would start handing operators a bearer they
  do not get today.
* **``/api/tokens`` is not a redaction site at all** — it is the one
  endpoint whose entire purpose is to serve plaintext bearers, so it
  gates (403) rather than masks. It shares the *who* predicate and
  nothing else.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Tuple


#: Full mask for a withheld bearer. Full, not a prefix/suffix elision:
#: the previous ``token[:4] + "..." + token[-4:]`` form disclosed 8
#: characters of a secret to a non-operator caller — enough to narrow a
#: brute force or confirm a guess (viewer-read-gating finding 3).
REDACTED_TOKEN = "***"


def agent_secret_columns() -> frozenset[str]:
    """The ``agents`` columns marked ``info={"secret": True}`` on the model.

    Derived, not declared here — see the module docstring. Resolved
    lazily so ``core`` does not take an import-time dependency on the
    ORM layer.
    """
    from ..db.models.agent import Agent

    return frozenset(
        column.key
        for column in Agent.__table__.columns
        if column.info.get("secret")
    )


def redact_agent_row(
    row: Mapping[str, Any], *, confirmed_operator_tier: bool,
) -> Dict[str, Any]:
    """Mask every secret column present in ``row`` for non-confirmed tiers.

    The agent-row twin of
    :func:`agent_mcp.tools.project_settings_tools.redact_settings_row`:
    a confirmed operator-tier caller gets the row unchanged; anyone else
    gets each secret column's value replaced by :data:`REDACTED_TOKEN`.
    Keys stay present, so a client can tell a masked value from an absent
    one. Non-secret columns always pass through with their real values.
    """
    out = dict(row)
    if confirmed_operator_tier:
        return out
    for key in agent_secret_columns():
        if key in out:
            out[key] = REDACTED_TOKEN
    return out


def strip_agent_secrets(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Drop every secret column from ``row``, for every caller tier.

    For surfaces whose contract has no secret field at all and whose row
    only carries one because it came from a ``SELECT *``. Unconditional
    by design: the caller that legitimately needs a bearer reads it from
    a separate, explicitly gated field (``/all-data``'s ``auth_token``)
    or a separate endpoint (``GET /api/tokens``), never from the raw
    column.
    """
    secrets = agent_secret_columns()
    return {k: v for k, v in row.items() if k not in secrets}


def without_secret_columns(columns: Iterable[str]) -> Tuple[str, ...]:
    """Filter a hand-chosen display-column list down to the non-secret ones.

    The ``/node-details`` projection is a *presentation* decision (it
    deliberately shows fewer columns than the model has), so it stays a
    hand-written allowlist rather than becoming "everything non-secret".
    Running it through here makes the security half structural anyway: a
    secret column added to that list is dropped rather than served.
    """
    secrets = agent_secret_columns()
    return tuple(c for c in columns if c not in secrets)


__all__ = [
    "REDACTED_TOKEN",
    "agent_secret_columns",
    "redact_agent_row",
    "strip_agent_secrets",
    "without_secret_columns",
]
