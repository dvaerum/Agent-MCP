"""SSO config + OIDC + proxy-header trust (Phase 3 Wave 3).

Two mutually-exclusive SSO modes layer on top of the Phase 1
username+password store:

  * **OIDC** — full authorization-code + PKCE flow against an external
    identity provider. The router becomes a Relying Party; one OIDC
    issuer is configured at a time (option A from the locked grilling
    in ``docs/plans/prancy-napping-pie.md``). On a successful callback
    we JIT-create the local ``users`` row (matched by ``email`` claim)
    and apply optional group-claim → agent-mcp-group mapping.

  * **proxy-header trust** — an upstream reverse proxy
    (nginx+oauth2-proxy, traefik+forward-auth, tailscale-serve+Tailnet
    identity, …) authenticates the request and forwards the username
    in a header (typically ``Remote-User``). The router treats that
    header as a session-equivalent identity. CRITICAL SAFETY RULE: the
    router MUST refuse to honour the header unless the request arrives
    from a configured trusted source (default: localhost). Without
    this enforcement a remote attacker could spoof the header and walk
    straight in.

Config travels via env vars (matching the rest of the router) so the
nix / sops deployment pattern lifts cleanly:

  OIDC:
    AGENT_MCP_SSO_OIDC_ISSUER             (turning this on activates OIDC)
    AGENT_MCP_SSO_OIDC_CLIENT_ID
    AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE — path to a chmod-0600 file
    AGENT_MCP_SSO_OIDC_PROVIDER_NAME      — display name on the button
    AGENT_MCP_SSO_OIDC_GROUP_MAPPING      — JSON {oidc_group: amcp_group}.
                                            "*" enables JIT-create.
    AGENT_MCP_SSO_OIDC_REDIRECT_URL       — override; defaults to
                                            "{EXTERNAL_URL}/agent-mcp/sso/callback"
    AGENT_MCP_SSO_OIDC_SCOPES             — space-separated scopes;
                                            default "openid profile email groups"

  Proxy-header:
    AGENT_MCP_SSO_PROXY_HEADER            (turning this on activates the mode)
    AGENT_MCP_SSO_PROXY_TRUSTED_IPS       — comma list; default
                                            "127.0.0.1,::1"
    AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN  — "true"/"false"; default false

ADR-0015 records the design rationale (single issuer; mapped + wildcard
group provisioning; localhost-only proxy trust by default).
"""

from __future__ import annotations

import enum
import functools
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web


__all__ = [
    "SSOConfigError",
    "SSOMode",
    "OIDCSettings",
    "ProxyHeaderSettings",
    "SSOSettings",
    "load_sso_config",
    "get_sso_config",
    "find_or_create_sso_user",
    "apply_group_mapping",
    "reconcile_oidc_group_membership",
    "is_trusted_proxy_source",
    "extract_proxy_header_user",
    "init_oidc_login_handler",
    "handle_oidc_callback",
    "register_sso_routes",
]


logger = logging.getLogger(__name__)


# ── Errors ──────────────────────────────────────────────────────────


class SSOConfigError(Exception):
    """Raised when the env-var SSO config is incoherent.

    Today the only enforced rule is the OIDC + proxy-header mutex; a
    future config layer (e.g. SAML when ADR-0016 lands) will reuse
    this error type.
    """


# ── Config dataclasses ──────────────────────────────────────────────


class SSOMode(str, enum.Enum):
    """The three possible authentication front-ends."""

    BUILTIN = "builtin"
    OIDC = "oidc"
    PROXY_HEADER = "proxy_header"


@dataclass(frozen=True)
class OIDCSettings:
    """Resolved OIDC RP settings.

    ``client_secret`` is the file CONTENTS, not the path — we read it
    once at config-load so the live router never re-reads from disk on
    every callback. The secret rotates by writing a new file and
    restarting the router (the same pattern the rest of the deploy
    follows for sops-managed secrets).
    """

    issuer: str
    client_id: str
    client_secret: str
    provider_name: str
    group_mapping: dict[str, str]
    redirect_url: str | None
    scopes: list[str]
    # Bootstrap gate for the OIDC sibling of the proxy-header
    # ``default_is_sysadmin`` fix (round-9 AC-R9-2). Gates ONLY the
    # empty-table FIRST-user sysadmin promotion — NOT every OIDC user
    # (unlike the proxy flag, which trusts the upstream gateway for
    # every request). Off by default: a fresh OIDC deploy's first IdP
    # user is NOT silently minted as sysadmin.
    default_is_sysadmin: bool = False


@dataclass(frozen=True)
class ProxyHeaderSettings:
    """Resolved proxy-header trust settings.

    ``trusted_ips`` is the parsed set of source-IP strings the router
    will trust to set ``trust_header``. Anything else gets the header
    silently dropped.
    """

    trust_header: str
    trusted_ips: frozenset[str]
    default_is_sysadmin: bool


@dataclass(frozen=True)
class SSOSettings:
    """The whole SSO surface — exactly one of (oidc, proxy) is set."""

    mode: SSOMode
    oidc: OIDCSettings | None
    proxy: ProxyHeaderSettings | None


# ── Config loading ──────────────────────────────────────────────────


_DEFAULT_SCOPES = ["openid", "profile", "email", "groups"]
_DEFAULT_TRUSTED_IPS = "127.0.0.1,::1"


def _env_truthy(value: str | None) -> bool:
    """Loose truthiness for env-var booleans."""
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_group_mapping(raw: str | None) -> dict[str, str]:
    """Parse the JSON mapping; return {} on missing / malformed."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "AGENT_MCP_SSO_OIDC_GROUP_MAPPING is not valid JSON; "
            "ignoring (no claim → group mapping will fire).",
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "AGENT_MCP_SSO_OIDC_GROUP_MAPPING must be a JSON object; "
            "got %s. Ignoring.", type(parsed).__name__,
        )
        return {}
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        out[k] = v
    return out


def _parse_trusted_ips(raw: str | None) -> frozenset[str]:
    """Comma-separated → frozenset of canonical IP strings.

    Garbage entries are dropped silently with a warning so a typo in
    one entry doesn't lock the operator out.
    """
    if not raw:
        return frozenset()
    out: set[str] = set()
    for raw_part in raw.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            addr = ipaddress.ip_address(part)
            out.add(str(addr))
        except ValueError:
            logger.warning(
                "AGENT_MCP_SSO_PROXY_TRUSTED_IPS: %r is not a valid IP "
                "address; dropping.", part,
            )
    return frozenset(out)


def load_sso_config() -> SSOSettings:
    """Read the env vars; return a fully-resolved SSOSettings.

    Raises ``SSOConfigError`` when both OIDC and proxy-header are
    configured simultaneously — the locked design admits exactly one
    SSO front-end at a time.
    """
    oidc_issuer = os.environ.get("AGENT_MCP_SSO_OIDC_ISSUER", "").strip()
    proxy_header = os.environ.get("AGENT_MCP_SSO_PROXY_HEADER", "").strip()

    if oidc_issuer and proxy_header:
        raise SSOConfigError(
            "Both AGENT_MCP_SSO_OIDC_ISSUER and "
            "AGENT_MCP_SSO_PROXY_HEADER are set. Pick one: OIDC "
            "(authorization-code flow against an IdP) OR proxy-header "
            "trust (upstream proxy populates the user identity). "
            "They are mutually exclusive."
        )

    if oidc_issuer:
        # Refuse a plaintext issuer unless the operator explicitly opts
        # in (OBS-R17-SSO). Production must run OIDC over https — an
        # http:// issuer means the discovery doc, token exchange and
        # JWKS all cross the network in the clear, MITM-able into an
        # attacker-chosen origin. Local-dev IdPs set
        # AGENT_MCP_SSO_OIDC_ALLOW_INSECURE=1 to opt back in.
        issuer_scheme = urlsplit(oidc_issuer).scheme.lower()
        allow_insecure = _env_truthy(
            os.environ.get("AGENT_MCP_SSO_OIDC_ALLOW_INSECURE"),
        )
        if issuer_scheme == "http":
            if not allow_insecure:
                raise SSOConfigError(
                    "AGENT_MCP_SSO_OIDC_ISSUER uses http://; OIDC "
                    "requires https. Set "
                    "AGENT_MCP_SSO_OIDC_ALLOW_INSECURE=1 to opt in for "
                    "a local-dev IdP."
                )
        elif issuer_scheme != "https":
            raise SSOConfigError(
                "AGENT_MCP_SSO_OIDC_ISSUER must be an http:// or "
                f"https:// URL; got scheme {issuer_scheme!r}."
            )

        client_id = os.environ.get("AGENT_MCP_SSO_OIDC_CLIENT_ID", "").strip()
        secret_file = os.environ.get(
            "AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE", "",
        ).strip()
        if not client_id:
            raise SSOConfigError(
                "AGENT_MCP_SSO_OIDC_ISSUER is set but "
                "AGENT_MCP_SSO_OIDC_CLIENT_ID is missing."
            )
        client_secret = ""
        if secret_file:
            try:
                client_secret = Path(secret_file).read_text().strip()
            except OSError as e:
                raise SSOConfigError(
                    f"AGENT_MCP_SSO_OIDC_CLIENT_SECRET_FILE={secret_file!r} "
                    f"could not be read: {e}"
                ) from e
        provider_name = (
            os.environ.get("AGENT_MCP_SSO_OIDC_PROVIDER_NAME", "").strip()
            or "SSO"
        )
        group_mapping = _parse_group_mapping(
            os.environ.get("AGENT_MCP_SSO_OIDC_GROUP_MAPPING"),
        )
        redirect_url = (
            os.environ.get("AGENT_MCP_SSO_OIDC_REDIRECT_URL", "").strip()
            or None
        )
        scopes_raw = os.environ.get(
            "AGENT_MCP_SSO_OIDC_SCOPES", "",
        ).strip()
        scopes = (
            scopes_raw.split() if scopes_raw else list(_DEFAULT_SCOPES)
        )
        oidc_default_sysadmin = _env_truthy(
            os.environ.get("AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN"),
        )
        return SSOSettings(
            mode=SSOMode.OIDC,
            oidc=OIDCSettings(
                issuer=oidc_issuer.rstrip("/"),
                client_id=client_id,
                client_secret=client_secret,
                provider_name=provider_name,
                group_mapping=group_mapping,
                redirect_url=redirect_url,
                scopes=scopes,
                default_is_sysadmin=oidc_default_sysadmin,
            ),
            proxy=None,
        )

    if proxy_header:
        trusted_ips = _parse_trusted_ips(
            os.environ.get(
                "AGENT_MCP_SSO_PROXY_TRUSTED_IPS", _DEFAULT_TRUSTED_IPS,
            )
        )
        default_sysadmin = _env_truthy(
            os.environ.get("AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN"),
        )
        return SSOSettings(
            mode=SSOMode.PROXY_HEADER,
            oidc=None,
            proxy=ProxyHeaderSettings(
                trust_header=proxy_header,
                trusted_ips=trusted_ips,
                default_is_sysadmin=default_sysadmin,
            ),
        )

    return SSOSettings(mode=SSOMode.BUILTIN, oidc=None, proxy=None)


_cached_config: SSOSettings | None = None


def get_sso_config(*, reload: bool = False) -> SSOSettings:
    """Return the process-cached SSOSettings, loading on first call.

    Tests that monkey-patch env vars after the first call can pass
    ``reload=True`` to force a re-read. The router's own routes use
    the cached value because env vars don't change at runtime in
    production.
    """
    global _cached_config
    if reload or _cached_config is None:
        _cached_config = load_sso_config()
    return _cached_config


def _reset_cache_for_tests() -> None:
    """Test-only: drop the cache so the next ``get_sso_config`` re-reads."""
    global _cached_config
    _cached_config = None


# ── JIT user creation ──────────────────────────────────────────────


_USERNAME_SANITISE = re.compile(r"[^a-z0-9-]+")

# Reserved namespace for wildcard-JIT'd OIDC groups. The ``:`` can't
# appear in a sanitised slug (the sanitiser collapses it to ``-``), so
# an ``oidc:``-prefixed group can never be produced by, or collide
# with, a locally-managed group slug — that's the anti-privilege-
# escalation invariant for the group-mapping wildcard path.
_WILDCARD_GROUP_PREFIX = "oidc:"

# Namespaces for the stable ``users.sso_subject`` reconciliation key,
# keeping OIDC subjects and proxy-header identities in disjoint spaces
# even if both modes leave rows in the same DB across a reconfigure.
_OIDC_SUBJECT_PREFIX = "oidc:"
_PROXY_SUBJECT_PREFIX = "proxy:"


# JSON-scalar shapes ``sub`` may legally arrive as (str is the spec
# shape; int/float/bool cover the real-world misconfigured-IdP cases
# R17-F1 was filed over, e.g. a numeric employee/subject id). A dict
# or list ``sub`` is not a sane identity key on its own -- degrade
# those to None the same as a missing claim, rather than keying
# reconciliation on a stringified blob.
_OIDC_SUBJECT_SCALAR_TYPES = (str, int, float, bool)


def _oidc_subject(iss: str | None, sub: object | None) -> str | None:
    """Build the stable OIDC subject key from ``(iss, sub)``.

    Per the OIDC spec ``sub`` is unique+stable only WITHIN an issuer,
    so both parts are needed. Returns None when either is missing (a
    spec-noncompliant id_token) — the caller then falls back to the
    verified-email / JIT-create path rather than keying on a partial
    identifier.

    ``sub`` deliberately accepts more than ``str``: this function only
    f-string-interpolates it, so any JSON scalar (str/int/float/bool)
    is safe input and produces a stable key. R17-F1: an earlier fix
    (R16-F1) mistakenly fed this the SAME str-only-coerced value used
    for the (genuinely ``str``-only) ``preferred_username`` fallback,
    which silently degraded a non-str ``sub`` (e.g. a numeric IdP
    subject id) to None on EVERY login -- defeating subject-based
    reconciliation and re-minting a fresh orphaned user each time
    instead of crashing. Only a missing ``sub`` or a non-scalar shape
    (dict/list, e.g. a misserialised multi-valued attribute) degrades
    to None here.
    """
    if not iss or sub is None:
        return None
    if not isinstance(sub, _OIDC_SUBJECT_SCALAR_TYPES):
        return None
    return f"{_OIDC_SUBJECT_PREFIX}{iss}:{sub}"


def _sanitise_username(raw: str) -> str:
    """Convert an arbitrary IdP-provided name to our slug shape.

    Lowercase, runs of non-[a-z0-9-] characters collapse to a single
    dash, leading / trailing dashes stripped. We accept whatever
    comes out — usernames in the router store are TEXT UNIQUE, not
    constrained by a regex.

    R16-F1 belt-and-suspenders: ``handle_oidc_callback`` now coerces a
    non-``str`` ``preferred_username``/``sub`` claim to ``None`` before
    it ever reaches here, so this guard should be unreachable via that
    caller — kept anyway since this is a module-level helper any future
    caller could reach directly with un-vetted input, and ``raw.lower()``
    on a dict/int is the exact crash this whole finding is about.
    """
    if not isinstance(raw, str):
        return "user"
    s = _USERNAME_SANITISE.sub("-", raw.lower())
    s = s.strip("-")
    return s or "user"


def _sanitise_group_name(raw: str) -> str:
    """Same shape as a username — groups share the slug convention."""
    return _sanitise_username(raw)


def find_or_create_sso_user(
    *,
    email: str | None,
    preferred_username: str | None,
    subject: str | None = None,
    email_verified: bool = False,
    default_is_sysadmin: bool = False,
    bootstrap_sysadmin: bool = False,
) -> dict[str, Any]:
    """Reconcile-by-subject, verified-email-link, or JIT-create; return the row.

    Matching algorithm (in order):

      1. **Stable subject.** If ``subject`` (OIDC ``(iss, sub)`` or the
         sanitised proxy-trusted username) matches an existing row's
         ``sso_subject``, return it. This is the ONLY reconciliation
         key for passwordless SSO rows — email is mutable / absent, so
         keying on it re-minted a new user (and, under
         ``AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN``, a fresh sysadmin) on
         every request.

      2. **Verified-email link.** If ``email`` is present AND the IdP
         asserted ``email_verified is True``, link to a PRE-EXISTING
         local account of that email — a password-backed operator, or
         a legacy (pre-subject) SSO row. The verification gate closes
         the account-takeover vector: an IdP that lets a user set an
         arbitrary UNVERIFIED email must not be able to seize a local
         operator of that email. Post-fix passwordless rows (which
         always carry a subject) are deliberately NOT email-matchable,
         so an attacker's unverified-email row can never be linked into
         by a later victim login. The subject is stamped onto the
         linked row so subsequent logins reconcile via step 1.

      3. **JIT-create.** A genuinely new subject → create a
         ``password_hash = NULL`` row (anchors the session only; no
         password login). The username collision-suffix (``-2``,
         ``-3``, …) now fires ONLY for genuinely different subjects —
         same-subject reconciliation already returned in step 1.

    ``default_is_sysadmin`` flips the sysadmin bit on the JIT row;
    only the proxy-header path passes True today, gated by
    ``AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN``.

    ``bootstrap_sysadmin`` (round-9 AC-R9-2) gates the separate
    empty-table FIRST-user sysadmin promotion inside
    ``_create_passwordless_user`` and DEFAULTS OFF. The OIDC callback
    threads its ``AGENT_MCP_SSO_OIDC_DEFAULT_SYSADMIN`` flag here so a
    fresh OIDC deploy's first IdP user is only auto-promoted when the
    operator opted in; the proxy path leaves the default (its sysadmin
    bit rides on ``default_is_sysadmin`` instead).
    """
    from . import identity

    # 1. Stable-subject reconciliation.
    if subject:
        existing = _find_user_by_subject(subject)
        if existing is not None:
            identity.touch_last_login(existing["user_id"])
            return existing

    # 2. Verified-email link to a pre-existing local account.
    if email and email_verified:
        linked = _find_linkable_user_by_email(email)
        if linked is not None:
            if subject:
                _stamp_subject_if_absent(linked["user_id"], subject)
            identity.touch_last_login(linked["user_id"])
            return identity.get_user_by_id(linked["user_id"]) or linked

    # 3. Genuinely new subject → JIT-create a passwordless row.
    base = _sanitise_username(preferred_username or (email or "user"))
    candidate = base
    suffix = 2
    while identity.get_user_by_username(candidate) is not None:
        candidate = f"{base}-{suffix}"
        suffix += 1

    # SSO-only row: no password at all (``password_hash`` stays NULL).
    # ``identity.create_user`` owns the first-user membership/sysadmin
    # bootstrap; the passwordless fork it used to have (this module's
    # ``_create_passwordless_user``) was deleted in arch-deepening R2 #1b.
    user_id = identity.create_user(
        username=candidate,
        password=None,
        email=email,
        password_hash=None,
        is_sysadmin=default_is_sysadmin,
        sso_subject=subject,
        bootstrap_sysadmin=bootstrap_sysadmin,
    )
    identity.touch_last_login(user_id)
    row = identity.get_user_by_id(user_id)
    assert row is not None, "freshly-created user should be readable"
    return row


def _find_user_by_subject(subject: str) -> dict[str, Any] | None:
    """Return the users row whose ``sso_subject`` == ``subject``, or None.

    Routes through the RouterStore (arch-deepening R2 #1c) so the router's
    user reads have one home instead of an inline ``sqlite3.connect``.
    """
    from .router_store import store

    return store.find_user_by_sso_subject(subject)


def _find_linkable_user_by_email(email: str) -> dict[str, Any] | None:
    """Return a LINK-eligible local row for ``email`` (case-insensitive).

    Only two row shapes are eligible link targets for a verified email:

      * a password-backed local operator (``password_hash IS NOT NULL``)
        — the account-linking feature's intended target, and
      * a legacy SSO row (``sso_subject IS NULL``) minted before the
        stable-subject column existed, so upgrading deployments keep
        reconciling their existing SSO users instead of duplicating.

    Post-fix passwordless SSO rows carry a non-NULL ``sso_subject`` and
    are excluded — that's what stops an attacker's unverified-email row
    from being linked into by a later victim login. Password users are
    preferred when both shapes share an email.

    Routes through the RouterStore (arch-deepening R2 #1c); the link
    predicate is unchanged from the inline query it replaces.
    """
    from .router_store import store

    return store.find_linkable_user_by_email(email)


def _stamp_subject_if_absent(user_id: str, subject: str) -> None:
    """Bind ``subject`` to ``user_id`` iff the row has no subject yet.

    Idempotent + race-safe against the partial UNIQUE index: the
    ``sso_subject IS NULL`` guard means a second, different subject can
    never overwrite an already-bound row, and the index rejects binding
    the same subject to two rows. Routes through the RouterStore
    (arch-deepening R2 #1c).
    """
    from .router_store import store

    store.stamp_sso_subject_if_absent(user_id, subject)


# ── Group mapping ──────────────────────────────────────────────────


def apply_group_mapping(
    user_id: str,
    group_claims: list[str],
    mapping: dict[str, str],
) -> set[str]:
    """Map OIDC group claims → agent-mcp groups; return the names added.

    Rules:

      * Explicit ``{oidc_name: amcp_name}`` mapping wins — if the
        named ``amcp_name`` group exists, the user becomes a member;
        if it doesn't exist, the entry is silently skipped (the
        sysadmin pre-creates groups before binding claims).
      * ``"*"`` in the mapping is the wildcard JIT escape — every
        unmatched claim auto-creates a sanitized agent-mcp group and
        the user is added. Wildcard-provisioned groups are NAMESPACED
        under the reserved ``oidc:`` prefix (e.g. claim ``admins`` →
        group ``oidc:admins``) so a claim value can never collide with
        — and silently inherit the capabilities / sysadmin bit of — a
        locally-managed group of the same slug. Explicit mappings are
        exempt: an operator who writes ``{"admins": "admins"}`` has
        deliberately opted into binding that claim to the local group.
      * Unmapped claims (no entry, no wildcard) are silently ignored.

    Idempotent: re-running with the same claims is a no-op for the
    group_membership rows that already exist.
    """
    from . import identity

    added: set[str] = set()
    wildcard = mapping.get("*")

    for claim in group_claims:
        if not isinstance(claim, str):
            continue
        target = mapping.get(claim)
        if target:
            group_name = target
        elif wildcard is not None:
            group_name = _WILDCARD_GROUP_PREFIX + _sanitise_group_name(claim)
        else:
            continue
        if not group_name:
            continue
        group_id = _ensure_group(group_name)
        if group_id is None:
            continue
        if _add_user_to_group_idempotent(group_id, user_id):
            added.add(group_name)
    return added


def _mapped_group_names(
    group_claims: list[str], mapping: dict[str, str],
) -> set[str]:
    """The set of agent-mcp group NAMES the current claims map to.

    Same mapping logic as ``apply_group_mapping`` (explicit entry wins,
    else the ``"*"`` wildcard produces an ``oidc:``-namespaced slug,
    else the claim is ignored) — but returns the FULL target set
    regardless of whether the user is already a member. Used by the
    de-provisioning reconciler to compute which IdP-managed memberships
    the current claim still justifies.
    """
    wildcard = mapping.get("*")
    out: set[str] = set()
    for claim in group_claims:
        if not isinstance(claim, str):
            continue
        target = mapping.get(claim)
        if target:
            name = target
        elif wildcard is not None:
            name = _WILDCARD_GROUP_PREFIX + _sanitise_group_name(claim)
        else:
            continue
        if name:
            out.add(name)
    return out


def reconcile_oidc_group_membership(
    user_id: str,
    group_claims: list[str],
    mapping: dict[str, str],
) -> set[str]:
    """Revoke IdP-managed group memberships the current claim no longer
    justifies; return the group names removed.

    De-provisioning counterpart to ``apply_group_mapping`` (round-9
    AC-R9-1). ``apply_group_mapping`` is additive-only, so a user
    dropped from an IdP group kept the local ``group_membership`` row —
    and, because ``group_resolver`` derives sysadmin / project-role
    transitively from those rows, kept the privilege indefinitely.

    SCOPING — CRITICAL: only the reserved ``oidc:`` namespace is
    reconciled. ``group_membership`` carries NO per-row provenance
    column (see migration 0002), so at the row level an IdP-derived
    grant is indistinguishable from a manual admin grant. The ONLY
    unambiguous IdP-sourced marker is the ``oidc:`` group-name prefix:
    those groups are provisioned EXCLUSIVELY by the wildcard-JIT path in
    ``apply_group_mapping``, so every membership in them is IdP-sourced
    and safe to revoke when the claim disappears. Everything else — an
    operator's manual grant to a locally-managed group, AND explicit-
    mapping target groups (arbitrary local slugs an operator bound a
    claim to, which a manual grant can also populate) — is left
    additive-only so an SSO login can never remove a manual grant.

    Idempotent: unchanged claims remove nothing (the still-claimed
    ``oidc:`` groups stay in the retained set).
    """
    claimed = _mapped_group_names(group_claims, mapping)
    claimed_oidc = {
        n for n in claimed if n.startswith(_WILDCARD_GROUP_PREFIX)
    }
    current_oidc = _user_oidc_group_memberships(user_id)
    removed: set[str] = set()
    for name, group_id in current_oidc.items():
        if name in claimed_oidc:
            continue
        if _remove_user_from_group(group_id, user_id):
            removed.add(name)
    return removed


def _user_oidc_group_memberships(user_id: str) -> dict[str, str]:
    """Return ``{group_name: group_id}`` for the user's DIRECT memberships
    in ``oidc:``-namespaced groups (the IdP-managed reconcile scope).

    Returns ``{}`` when the groups tables are absent (backlevel deploy),
    matching the silent-skip posture of the other group helpers. Routes
    through the RouterStore (arch-deepening R2 #1c); the
    ``OperationalError``-swallow stays here so the backlevel-deploy
    tolerance is unchanged.
    """
    from .router_store import store

    try:
        return store.user_group_memberships_by_name_prefix(
            user_id, _WILDCARD_GROUP_PREFIX,
        )
    except sqlite3.OperationalError:
        return {}


def _remove_user_from_group(group_id: str, user_id: str) -> bool:
    """Delete a user→group edge; return True iff a row was removed.

    Routes through the RouterStore (arch-deepening R2 #1c); the
    ``OperationalError``-swallow (backlevel deploy) stays here.
    """
    from .router_store import store

    try:
        return store.remove_group_member(group_id, user_id)
    except sqlite3.OperationalError:
        return False


def _ensure_group(name: str) -> str | None:
    """Return the group_id for ``name``, JIT-creating if missing.

    Returns None if the schema doesn't have the groups table — the
    Phase-3 migrations haven't run, which means the operator is on a
    backlevel deploy and we should silently skip group provisioning
    rather than 500 the callback. Routes through the RouterStore
    (arch-deepening R2 #1c); the ``OperationalError``-swallow stays here.
    """
    from .router_store import store

    try:
        return store.ensure_group(name)
    except sqlite3.OperationalError:
        return None


def _add_user_to_group_idempotent(group_id: str, user_id: str) -> bool:
    """Insert a user→group edge; return True iff a row was added.

    The INSERT routes through ``store.add_group_member`` (arch-deepening
    R2 #1b) so ``group_membership`` has one writer. The pre-check keeps
    the idempotent "was it newly added?" return contract
    ``apply_group_mapping`` depends on, and the ``OperationalError``
    fallback still silently no-ops on a backlevel deploy whose groups
    tables haven't been migrated in.
    """
    from . import identity
    from .router_store import store

    try:
        with identity._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM group_membership WHERE group_id = ? "
                "AND member_user_id = ?", (group_id, user_id),
            )
            if cur.fetchone() is not None:
                return False
            store.add_group_member(
                group_id, member_user_id=user_id, conn=conn,
            )
            return True
    except sqlite3.OperationalError:
        return False


# ── Proxy-header trust helpers ─────────────────────────────────────


def _users_table_is_empty() -> bool:
    """True iff no operator account exists yet (fresh-deploy state).

    Delegates to ``store.users_table_is_empty`` — the single empty-table
    probe (arch-deepening R2 #1c). Imported lazily to keep sso's import
    graph light.
    """
    from .router_store import store

    return store.users_table_is_empty()


def is_trusted_proxy_source(
    request: web.Request, settings: ProxyHeaderSettings,
) -> bool:
    """Return True iff this request originates from a trusted source IP.

    The peer IP comes from aiohttp's ``request.remote`` (which honours
    the transport's reported peername; we deliberately do NOT consult
    ``X-Forwarded-For`` for trust decisions — the forwarded chain is
    operator-supplied and the trusted-IP check IS the gatekeeper that
    prevents header spoofing).
    """
    peer = request.remote or ""
    if not peer:
        return False
    try:
        parsed = ipaddress.ip_address(peer)
    except ValueError:
        return False
    canonical = str(parsed)
    return canonical in settings.trusted_ips


def extract_proxy_header_user(
    request: web.Request, settings: ProxyHeaderSettings,
) -> dict[str, Any] | None:
    """Resolve (or JIT-create) the user identified by the trusted header.

    Returns None when:

      * the header is absent or empty,
      * the request didn't originate from a trusted source (the header
        is silently dropped — we don't log per-request, but
        ``is_trusted_proxy_source`` is auditable).

    On success, returns the user dict (suitable for stashing on
    ``request['user']`` as the cookie path does).
    """
    if not is_trusted_proxy_source(request, settings):
        return None
    raw = request.headers.get(settings.trust_header, "").strip()
    if not raw:
        return None
    # Bootstrap gate: on an EMPTY users table, only auto-mint the first
    # user when the operator opted into proxy auto-sysadmin. With
    # ``DEFAULT_SYSADMIN=false`` the operator explicitly declined it, so
    # the first admin must be minted through the setup wizard instead —
    # JIT-creating a non-sysadmin passwordless row here would both
    # violate the flag AND make the users table non-empty, locking the
    # wizard away (it only renders while the table is empty). Returning
    # None leaves the caller unauthenticated; HTML paths then 303 to
    # /setup via the empty-users middleware.
    if not settings.default_is_sysadmin and _users_table_is_empty():
        return None
    # Stable subject for the proxy path is the RAW trusted username,
    # namespaced so it can't collide with an OIDC ``sub``. It MUST be
    # the raw (un-sanitised) value: ``_sanitise_username`` collapses
    # every run of non-[a-z0-9-] to a single dash, so ``a.b@corp``,
    # ``a-b@corp`` and ``a_b@corp`` would all slugify to one subject and
    # the second principal would reconcile INTO the first's account
    # (inheriting its groups/grants — a login-as regression). Keying on
    # the raw header value keeps distinct upstream principals distinct;
    # sanitisation is applied ONLY to the display username below.
    # Without a stable subject every request (no session cookie in proxy
    # mode) would re-mint a fresh ``name``/``name-2``/… row with grants
    # that never stick.
    subject = _PROXY_SUBJECT_PREFIX + raw
    return find_or_create_sso_user(
        email=None,
        preferred_username=raw,
        subject=subject,
        default_is_sysadmin=settings.default_is_sysadmin,
    )


# ── OIDC flow ──────────────────────────────────────────────────────


_FLOW_COOKIE_NAME = "agent_mcp_sso_flow"
_FLOW_COOKIE_PATH = "/agent-mcp/sso/"
_FLOW_COOKIE_MAX_AGE = 10 * 60  # 10 minutes — plenty for the round-trip


@dataclass(frozen=True)
class _FlowState:
    state: str
    code_verifier: str
    nonce: str


def _encode_flow_cookie(state: _FlowState) -> str:
    """Encode the per-flow state as a single cookie value.

    The state + PKCE verifier + nonce are bound to the operator's
    browser via an opaque cookie (so a phishing IdP can't replay
    another user's in-flight state). The nonce additionally binds the
    returned id_token to this auth attempt (OIDC anti-replay). We pack
    as base64url(JSON) since all fields are short ASCII strings.
    """
    import base64
    payload = json.dumps({
        "state": state.state,
        "verifier": state.code_verifier,
        "nonce": state.nonce,
    }).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_flow_cookie(raw: str) -> _FlowState | None:
    import base64
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = base64.urlsafe_b64decode(padded.encode())
        parsed = json.loads(data)
        # Fail closed on a missing/empty nonce. Authlib's validate_nonce
        # is gated on `if nonce_value:` — an EMPTY expected nonce skips
        # the comparison entirely, so an id_token minted for a DIFFERENT
        # auth request would be accepted. The flow cookie is unsigned
        # base64(JSON), hence attacker-craftable, so a nonce-less cookie
        # MUST be treated as an invalid flow rather than one that
        # silently disables anti-replay (round-3 finding AC-1).
        nonce = parsed.get("nonce", "")
        if not nonce:
            return None
        return _FlowState(
            state=parsed["state"],
            code_verifier=parsed["verifier"],
            nonce=nonce,
        )
    except Exception:
        return None


def _default_redirect_url(request: web.Request) -> str:
    """Build a redirect_uri rooted at the same host the operator hit.

    Honours ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` so the URL
    handed to the IdP matches what the operator saw in the address
    bar (the IdP enforces an exact match against the registered
    redirect URI).

    Security (OBS7 class-sweep): the forwarding headers are
    client-settable, so they are trusted ONLY when the direct peer is a
    trusted proxy (``rate_limit.request_from_trusted_proxy``); an
    untrusted hit falls back to the real transport values. The IdP's
    exact registered-URI match already backstops a forged host, but the
    gate keeps this site consistent with ``login._external_origin``.
    Reached only when neither ``AGENT_MCP_SSO_OIDC_REDIRECT_URL`` nor
    ``AGENT_MCP_EXTERNAL_URL`` is set (see ``_resolve_redirect_url``).
    """
    from . import rate_limit

    proto = request.url.scheme
    host = request.host
    if rate_limit.request_from_trusted_proxy(request):
        proto = request.headers.get("X-Forwarded-Proto") or proto
        host = request.headers.get("X-Forwarded-Host") or host
    return f"{proto}://{host}/agent-mcp/sso/callback"


def _resolve_redirect_url(
    request: web.Request, settings: OIDCSettings,
) -> str:
    if settings.redirect_url:
        return settings.redirect_url
    external = os.environ.get("AGENT_MCP_EXTERNAL_URL", "").rstrip("/")
    if external:
        return f"{external}/agent-mcp/sso/callback"
    return _default_redirect_url(request)


# ── Authlib seam (monkey-patchable for tests) ──────────────────────


# Discovery endpoints whose origin we pin to the configured issuer.
# A hostile / compromised IdP (or a MITM against an http:// issuer)
# could otherwise redirect these at internal hosts (SSRF-ish): the
# token/JWKS fetches and the authorize redirect all trust these URLs
# verbatim. OBS-R17-SSO.
_ORIGIN_PINNED_ENDPOINTS = (
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
)

_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


def _origin_tuple(url: str) -> tuple[str, str | None, int | None]:
    """(scheme, host, effective-port) — the same-origin identity of a URL.

    The port is normalised to its scheme default when absent, so an
    explicit ``:443`` on an https URL compares equal to a port-less one.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = (
        parts.port if parts.port is not None
        else _DEFAULT_SCHEME_PORTS.get(scheme)
    )
    return (scheme, parts.hostname, port)


def _assert_discovery_same_origin(
    issuer: str, metadata: dict[str, Any],
) -> None:
    """Reject a discovery doc whose trusted endpoints leave the issuer origin.

    Origin-pin (OBS-R17-SSO): ``authorization_endpoint`` /
    ``token_endpoint`` / ``jwks_uri`` MUST each share the configured
    issuer's scheme + host (+ port). A mismatch — or a missing endpoint —
    raises ``SSOConfigError``, failing the SSO flow rather than trusting
    an attacker-controlled URL. Does NOT touch the JOSE / iss / aud /
    nonce checks, which validate the token independently.
    """
    issuer_origin = _origin_tuple(issuer)
    for key in _ORIGIN_PINNED_ENDPOINTS:
        endpoint = metadata.get(key)
        if not isinstance(endpoint, str) or not endpoint:
            raise SSOConfigError(
                f"OIDC discovery document is missing a usable {key!r}."
            )
        if _origin_tuple(endpoint) != issuer_origin:
            raise SSOConfigError(
                f"OIDC discovery {key!r} origin does not match the "
                f"configured issuer origin; refusing (OBS-R17-SSO "
                f"origin-pin)."
            )


def _fetch_oidc_metadata(issuer: str) -> dict[str, Any]:
    """Fetch the OIDC discovery document and origin-pin its endpoints.

    Sync call (we use requests for parity with Authlib's sync surface);
    the route handler runs this in an executor so the event loop
    doesn't block on the network. The fetched endpoints are validated
    against the configured issuer origin before being returned so both
    route handlers inherit the check at the single trust boundary.
    """
    import requests
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    metadata = resp.json()
    _assert_discovery_same_origin(issuer, metadata)
    return metadata


def _exchange_code_for_tokens(
    *,
    settings: OIDCSettings,
    metadata: dict[str, Any],
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """POST the authorization code to the IdP's token endpoint.

    Wraps Authlib's ``OAuth2Session.fetch_token``; the sync transport
    matches the rest of this module's IdP-facing calls.
    """
    from authlib.integrations.requests_client import OAuth2Session
    sess = OAuth2Session(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=redirect_uri,
        scope=" ".join(settings.scopes),
    )
    token = sess.fetch_token(
        url=metadata["token_endpoint"],
        code=code,
        code_verifier=code_verifier,
        grant_type="authorization_code",
    )
    return dict(token)


def _decode_id_token(
    token: str,
    metadata: dict[str, Any],
    client_id: str,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Decode + validate the OIDC id_token; return the claims dict.

    Uses Authlib's JWS surface against the IdP's JWKS — signature
    verification is mandatory in production. Tests monkey-patch this
    function to skip the network fetch + signature math.
    """
    from authlib.jose import jwt
    from authlib.oidc.core import CodeIDToken
    import requests

    jwks_resp = requests.get(metadata["jwks_uri"], timeout=10)
    jwks_resp.raise_for_status()
    claims = jwt.decode(
        token,
        key=jwks_resp.json(),
        claims_cls=CodeIDToken,
        claims_options={
            "iss": {"essential": True, "value": metadata["issuer"]},
            "aud": {"essential": True, "value": client_id},
        },
        claims_params={"nonce": nonce},
    )
    claims.validate()
    return dict(claims)


# ── Route handlers ─────────────────────────────────────────────────


async def init_oidc_login_handler(request: web.Request) -> web.StreamResponse:
    """GET /agent-mcp/sso/login → 303 to the IdP's authorize endpoint.

    Builds a per-flow state + PKCE verifier, stashes them in an
    opaque cookie scoped to ``/agent-mcp/sso/``, then redirects the
    browser at the IdP. The cookie is consumed (and cleared) by the
    callback handler.
    """
    import asyncio
    settings = get_sso_config()
    if settings.mode is not SSOMode.OIDC or settings.oidc is None:
        raise web.HTTPNotFound()
    cfg = settings.oidc

    try:
        metadata = await asyncio.to_thread(_fetch_oidc_metadata, cfg.issuer)
    except Exception:
        # Full detail (issuer URL, network/TLS specifics) is retained in
        # the server log; the client gets a static, non-reflective body
        # so an unauthenticated browser can't probe the IdP topology
        # (SD-R10-1 error-hygiene sweep).
        logger.exception("OIDC discovery fetch failed for %s", cfg.issuer)
        return web.Response(
            status=502,
            text="OIDC discovery failed",
            content_type="text/plain",
        )

    from authlib.integrations.requests_client import OAuth2Session

    code_verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(32)
    redirect_uri = _resolve_redirect_url(request, cfg)
    sess = OAuth2Session(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        redirect_uri=redirect_uri,
        scope=" ".join(cfg.scopes),
        code_challenge_method="S256",
    )
    url, state = sess.create_authorization_url(
        metadata["authorization_endpoint"],
        code_verifier=code_verifier,
        nonce=nonce,
    )

    cookie_value = _encode_flow_cookie(
        _FlowState(
            state=state, code_verifier=code_verifier, nonce=nonce,
        ),
    )
    response = web.HTTPSeeOther(location=url)
    response.set_cookie(
        _FLOW_COOKIE_NAME, cookie_value,
        path=_FLOW_COOKIE_PATH,
        httponly=True,
        secure=_cookie_secure_flag(request),
        samesite="Lax",
        max_age=_FLOW_COOKIE_MAX_AGE,
    )
    raise response


def _cookie_secure_flag(request: web.Request) -> bool:
    """Same heuristic as ``login.cookie_secure_flag`` — kept local so
    this module doesn't take a hard import on the login submodule.

    Includes the same fail-closed override: when
    ``AGENT_MCP_REQUIRE_SECURE_COOKIES`` is set the flag is always
    True so no SSO session / flow cookie is ever issued without
    ``Secure``.

    Honours ``X-Forwarded-Proto`` only when the direct peer is a
    trusted proxy (``rate_limit.request_from_trusted_proxy``) — the
    header is client-settable, so an untrusted peer must not drive the
    Secure decision (R6-F3, pentest-all round 6: OBS7 class-sweep miss
    — this module's docstring already claimed the "same heuristic" as
    ``login.cookie_secure_flag`` but never actually applied the gate).
    """
    require = os.environ.get("AGENT_MCP_REQUIRE_SECURE_COOKIES")
    if require is not None and require.strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return True
    from agent_mcp.router.rate_limit import request_from_trusted_proxy
    if request_from_trusted_proxy(request):
        forwarded = request.headers.get("X-Forwarded-Proto", "").lower()
        if forwarded == "https":
            return True
        if forwarded == "http":
            return False
    return request.url.scheme == "https"


async def handle_oidc_callback(request: web.Request) -> web.StreamResponse:
    """GET /agent-mcp/sso/callback?code=…&state=… → mint session cookie."""
    import asyncio
    settings = get_sso_config()
    if settings.mode is not SSOMode.OIDC or settings.oidc is None:
        raise web.HTTPNotFound()
    cfg = settings.oidc

    state_param = request.rel_url.query.get("state", "")
    code_param = request.rel_url.query.get("code", "")
    flow_cookie = request.cookies.get(_FLOW_COOKIE_NAME, "")

    if not state_param or not code_param or not flow_cookie:
        return web.Response(status=400, text="missing oidc callback params")
    flow = _decode_flow_cookie(flow_cookie)
    if flow is None or flow.state != state_param:
        return web.Response(status=400, text="invalid oidc state")
    # Defence in depth: never hand Authlib an empty expected nonce (its
    # validate_nonce no-ops on a falsy value). _decode_flow_cookie
    # already rejects a nonce-less cookie, so this is a redundant guard
    # kept explicit at the trust boundary (round-3 finding AC-1).
    if not flow.nonce:
        return web.Response(status=400, text="invalid oidc flow")

    try:
        metadata = await asyncio.to_thread(_fetch_oidc_metadata, cfg.issuer)
    except Exception:
        # Static client body; issuer URL + failure detail stay in the log
        # (SD-R10-1).
        logger.exception("OIDC discovery fetch failed during callback")
        return web.Response(
            status=502, text="OIDC discovery failed",
        )

    redirect_uri = _resolve_redirect_url(request, cfg)
    try:
        token = await asyncio.to_thread(
            _exchange_code_for_tokens,
            settings=cfg,
            metadata=metadata,
            code=code_param,
            redirect_uri=redirect_uri,
            code_verifier=flow.code_verifier,
        )
    except Exception:
        # Token-endpoint URL + IdP error prose stay server-side (SD-R10-1).
        logger.exception("OIDC token exchange failed")
        return web.Response(
            status=502, text="OIDC token exchange failed",
        )

    id_token = token.get("id_token", "")
    if not id_token:
        return web.Response(
            status=502, text="OIDC token response missing id_token",
        )
    try:
        claims = await asyncio.to_thread(
            _decode_id_token, id_token, metadata, cfg.client_id,
            flow.nonce,
        )
    except Exception:
        # JWKS URL + validation internals stay server-side (SD-R10-1).
        logger.exception("OIDC id_token decode failed")
        return web.Response(
            status=502, text="OIDC id_token validation failed",
        )

    # R16-F1: OIDC claims are untyped/optional per spec -- a
    # misconfigured IdP (a multi-valued LDAP/SCIM attribute serialised
    # as a JSON array/object) can send a non-``str`` value for
    # ``email``/``preferred_username``. Coerce a badly-typed claim to
    # None (same "degrade to absent" posture the ``groups_claim`` guard
    # just below already uses for a non-list ``groups``) so it falls
    # through to JIT-create with a generated username / the existing
    # ``InvalidEmailError``->502 path, instead of propagating an
    # unhandled ``AttributeError``/``sqlite3.ProgrammingError`` out of
    # ``identity.create_user``, ``_sanitise_username``, or
    # ``find_linkable_user_by_email`` -- all of which assume ``str``.
    # R17-F1: ``sub`` is deliberately NOT included in that "degrade
    # non-str to None" posture below -- see ``_oidc_subject``'s
    # docstring for why the stable-identity-key use of ``sub`` needs a
    # wider (JSON-scalar) type acceptance than the username-fallback
    # use does.
    raw_email = claims.get("email")
    email = raw_email if isinstance(raw_email, str) else None
    # Strict boolean check: an IdP that omits the claim, or sends a
    # string / falsy value, is treated as UNVERIFIED so its email can't
    # be used to link to (take over) a pre-existing local account.
    email_verified = claims.get("email_verified") is True
    raw_preferred_username = claims.get("preferred_username")
    preferred_username_claim = (
        raw_preferred_username if isinstance(raw_preferred_username, str) else None
    )
    raw_sub = claims.get("sub")
    # ``sub_claim`` is the str-only coercion, used ONLY for the
    # preferred_username fallback below (``_sanitise_username`` needs a
    # ``str`` for ``.lower()``). ``_oidc_subject`` gets the RAW claim
    # instead (R17-F1) -- it accepts any JSON scalar and is the ONLY
    # input to the stable reconciliation key, so degrading a non-str
    # (but still scalar, e.g. numeric) sub to None here would silently
    # defeat subject-based account reconciliation on every login.
    sub_claim = raw_sub if isinstance(raw_sub, str) else None
    preferred_username = preferred_username_claim or sub_claim
    # Stable reconciliation key: (iss, sub). ``iss`` is the validated
    # issuer from the id_token; fall back to the configured issuer.
    subject = _oidc_subject(claims.get("iss") or cfg.issuer, raw_sub)
    groups_claim = claims.get("groups") or []
    if not isinstance(groups_claim, list):
        groups_claim = []

    from . import identity

    try:
        user = find_or_create_sso_user(
            email=email,
            preferred_username=preferred_username,
            subject=subject,
            email_verified=email_verified,
            # Bootstrap gate (round-9 AC-R9-2): the empty-table first-user
            # sysadmin promotion only fires when the operator opted in. The
            # empty-users redirect middleware already bounces empty-table
            # HTTP logins to /setup, so this is the code-level backstop that
            # keeps a fresh OIDC deploy from silently minting a sysadmin if
            # the callback is ever reached against an empty table.
            bootstrap_sysadmin=cfg.default_is_sysadmin,
        )
    except identity.InvalidEmailError:
        # R15-F2 sibling: an IdP ``email`` claim carrying an unpaired
        # UTF-16 surrogate would otherwise crash ``create_user``'s INSERT
        # with a raw ``UnicodeEncodeError``, surfacing as a bare 500 —
        # same posture as the discovery/token/decode failures above:
        # IdP-side data problem, log server-side, clean 502 to the client.
        logger.exception("OIDC claims produced an invalid email")
        return web.Response(
            status=502, text="OIDC claims contained invalid email content",
        )
    if cfg.group_mapping:
        apply_group_mapping(
            user["user_id"], groups_claim, cfg.group_mapping,
        )
        # De-provision (round-9 AC-R9-1): revoke IdP-managed (oidc:)
        # memberships the current claim no longer justifies, so shrinking
        # IdP group membership revokes the corresponding local privilege.
        # Manual local grants are out of scope and untouched.
        reconcile_oidc_group_membership(
            user["user_id"], groups_claim, cfg.group_mapping,
        )

    # Mint the operator session cookie via the existing helper path.
    from . import identity
    from .login import SESSION_COOKIE_NAME, COOKIE_PATH, COOKIE_MAX_AGE

    session_id = identity.create_session(user["user_id"])
    identity.touch_last_login(user["user_id"])

    response = web.HTTPSeeOther(location="/agent-mcp/")
    response.set_cookie(
        SESSION_COOKIE_NAME, session_id,
        path=COOKIE_PATH,
        httponly=True,
        secure=_cookie_secure_flag(request),
        samesite="Lax",
        max_age=COOKIE_MAX_AGE,
    )
    # Clear the per-flow cookie now that we've consumed it.
    response.set_cookie(
        _FLOW_COOKIE_NAME, "",
        path=_FLOW_COOKIE_PATH,
        httponly=True,
        secure=_cookie_secure_flag(request),
        samesite="Lax",
        max_age=0,
    )
    raise response


# ── Wire-up ────────────────────────────────────────────────────────


def register_sso_routes(app: web.Application) -> None:
    """Register /agent-mcp/sso/{login,callback} on ``app``.

    Always-registered (we don't gate the route on OIDC mode being
    active) because the handlers themselves 404 cleanly when SSO is
    off; this keeps URL discoverability consistent for dashboards
    that probe for the route.
    """
    app.router.add_get(
        "/agent-mcp/sso/login", init_oidc_login_handler,
    )
    app.router.add_get(
        "/agent-mcp/sso/callback", handle_oidc_callback,
    )
