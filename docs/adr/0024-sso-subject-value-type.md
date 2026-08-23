# ADR 0024: The OIDC subject key is a value type, not an f-string

**Status**: Accepted, 2026-08-23. Supersedes ADR-0015's "User matching
algorithm" bullet (and the proxy-header section's "same algorithm as
OIDC's" reference to it), which had drifted from the code for six
pentest rounds.
**Date**: 2026-08-23
**Builds on**: ADR-0013 (operator login — the `users` table this keys
into), ADR-0015 (SSO via OIDC + proxy-header trust — the flow this sits
inside).

## Context

ADR-0015 shipped SSO with a one-line matching rule: *match by `email`
claim → existing `users.email`, else JIT-create*. That rule is no longer
what the code does, and hasn't been since the first pentest round that
touched it. Email is mutable and IdP-asserted, so matching on it
re-minted a user (and, under
`AGENT_MCP_SSO_PROXY_DEFAULT_SYSADMIN`, a fresh sysadmin) on every
request, and let an IdP with unverified emails seize a local operator
account. The reconciliation key became a stable `(iss, sub)` pair
persisted in `users.sso_subject`, matched before email, with the email
path demoted to a verified-only link into pre-existing rows.

That key was built by a bare f-string, and **five consecutive
`/pentest-all` rounds landed on it — each round's fix seeding the next
round's finding**:

| Round | PR | Finding |
|---|---|---|
| R16-F1 | #704 | A non-`str` `sub` (a misserialised multi-valued IdP attribute) crashed the callback. Fixed by coercing `sub` to `str`-or-`None`. |
| R17-F1 | #705 | …but that coercion was applied to the *identity key* too, so a numeric IdP subject id degraded to `None` on **every** login and re-minted an orphan row each time. Fixed by widening the key to accept any JSON scalar. |
| R18-F1 | #708 | …but the widened key still interpolated `sub` bare, and `str()` is not type-discriminating: `sub=True` and `sub="True"` (also `1`/`"1"`, `1.0`/`"1.0"`) collapsed onto one key, so a second, genuinely distinct claimant silently reconciled into the first's account. Fixed by embedding `type(sub).__name__` in the key. (Also: `sub == ""` was treated as present.) |
| R19-F1 | #709 | …but retagging the key orphaned every **pre-existing** SSO row: the tagged lookup missed, the email path refuses rows that already carry a subject, so a real user got a fresh unprivileged account on their next login. Fixed by a fallback lookup on the old untagged format, self-healing the row forward on a hit. |
| R20-F1 | #710 | …but that fallback matched on pure string equality and unconditionally re-stamped the row to the *current* caller's identity — a live-confirmed **account takeover**: a brand-new identity inherited an existing sysadmin row on first login. Fixed by refusing the fallback whenever the sub is type-ambiguous. |

Read as a sequence, this is not five unrelated bugs. It is one
representation being asked to carry four different responsibilities at
once, with no seam between them:

* **(a) the reconciliation key** persisted in `users.sso_subject`,
* **(b) the scalar-type discriminator** that keeps `True` and `"True"`
  apart,
* **(c) the pre-R18-F1 untagged legacy format** used to find rows
  written before (b) existed, and
* **(d) the ambiguity rule** that decides when (c) may be used at all.

Every one of those lived inside one interpolation plus two free
functions that took `object` and returned `str | None`. Nothing in the
type system, and nothing testable in isolation, said which
responsibility a given call was exercising — so each fix reached for the
nearest string operation and the next round found the seam it missed.
The architecture review (`docs/proposals/security-authz-architecture-hardening.md`,
Finding C) called this out as the ledger's clearest "the representation
itself is unsafe, not just this one bug" case.

## Decision

**`agent_mcp/router/sso.py` grows a frozen `SsoSubject` value type, and
the four responsibilities become four members of it.** The f-string
helpers (`_oidc_subject`, `_oidc_subject_legacy`,
`_legacy_subject_is_ambiguous`, `_looks_like_canonical_int/_float`) are
deleted; the OIDC callback asks the value for its lookup keys.

```python
@dataclass(frozen=True, eq=False)
class SsoSubject:
    iss: str
    sub: str | int | float | bool

    @classmethod
    def from_claims(cls, iss, sub) -> SsoSubject | None   # boundary
    @property
    def type_tag(self) -> str                             # (b)
    def encode(self) -> str                               # (a)
    @classmethod
    def decode(cls, encoded: str) -> SsoSubject | None    # (a) inverse
    def is_ambiguous(self) -> bool                        # (d)
    def legacy_lookup_key(self) -> str | None             # (c), gated by (d)
```

Five properties are load-bearing:

1. **The wire format does not change.** `encode()` emits exactly
   `oidc:<iss>:<type>:<sub>`, byte-for-byte what R18-F1 shipped. The
   encoded string is a *persisted DB value*; changing its bytes orphans
   every existing SSO row — that is literally the R19-F1 bug — so a
   golden-literal test pins it and any future change to it is a
   migration, not a refactor.

2. **Construction enforces the acceptance rules**, so no call site can
   build an unusable subject: non-empty `str` issuer; `sub` restricted
   to the JSON scalars (R17-F1's widening, still refusing dict/list);
   empty-string `sub` refused like a missing one (R18-F1). The boundary
   helper `from_claims()` returns `None` instead of raising, because at
   the IdP boundary "unusable claim" means "fall through to the
   verified-email / JIT-create path".

3. **Equality is type-exact.** Python's own `==` collapses
   `True == 1 == 1.0`; a value type that inherited that would re-open
   R18-F1 for any dict/set-based caller, so `__eq__`/`__hash__` compare
   `(iss, type_tag, sub)`.

4. **`decode()` is total and non-inventing.** For any input it returns
   either `None` or a subject that re-encodes to the *identical* bytes
   (the implementation re-encodes each candidate split and rejects any
   mismatch), so a stored row can never be attributed to a subject that
   would have been persisted under a different key. `decode(encode(x))
   == x` exactly, for any issuer that does not itself embed a
   `:<type-tag>:` marker — real issuers are https URLs and cannot. This
   is what makes the format testable as a property (600 fuzzed pairs)
   rather than by re-reading an f-string.

5. **One parser answers "could this type have produced this content?"**
   `_parse_tagged_scalar(tag, raw)` is used by *both* `decode()` and
   `is_ambiguous()`. That question having had two independent
   implementations is what let R19-F1's fallback and R18-F1's tag drift
   apart into R20-F1; now the canonical-repr rules (`"007"` is not a
   possible `str(int)`, `"1"` is not a possible `str(float)`, `"true"`
   is not a possible `str(bool)`) exist once.

### What is deliberately unchanged

This is a **representation** change, not a policy change. Every property
R18-F1/R19-F1/R20-F1 proved survives verbatim, each with a test that now
runs through the typed path:

* Distinct claim *types* never share a key (R18-F1).
* A pre-existing untagged row is still found by the fallback and still
  self-heals forward exactly once (R19-F1).
* The fallback is withheld — `legacy_lookup_key()` returns `None` —
  whenever the sub's untagged content could have been produced by
  another accepted scalar type: **any** non-`str` sub unconditionally,
  and a `str` sub iff its content is a canonical int/float repr or
  exactly `"True"`/`"False"` (R20-F1, both directions). The accepted
  cost is unchanged: a genuine legacy user whose sub happens to be
  numeric- or bool-shaped never self-heals and gets a fresh JIT row.

Two edge behaviours converge as a side effect, both strictly
fail-closed and both unreachable on the live path:

* A non-`str` `iss` claim now yields no subject instead of keying an
  account on a `repr()`. Authlib validates `iss` against the
  origin-pinned discovery issuer before these claims are returned, so
  this cannot occur in production.
* An empty-string `sub` now yields no *legacy* key either (previously
  the legacy builder accepted `""` while the current-format builder
  rejected it). `find_or_create_sso_user` only consults the legacy key
  inside `if subject:`, so the old asymmetry was already dead.

### Also folded in: `sso._cookie_secure_flag`

`sso._cookie_secure_flag` was a self-declared verbatim copy of
`login.cookie_secure_flag` ("kept local so this module doesn't take a
hard import on the login submodule"). It had already drifted once and
produced **R6-F3** (#673): the copy claimed the "same heuristic" but
never applied the trusted-proxy gate to `X-Forwarded-Proto`, so an
untrusted peer could drive the `Secure` decision. The bodies were
re-verified identical before removal; the function is now a one-line
delegation to the canonical implementation, keeping the SSO-path
regression tests pointed at the SSO path while leaving exactly one copy
of the rule. The stated cost (a module import) is a lazy function-local
import — the same thing this module already does for `login`'s cookie
constants a few lines below.

## Consequences

### Positive

* The four responsibilities are separately nameable, separately
  testable, and separately documented. A future fix has an obvious place
  to land instead of a string to extend.
* The persisted format is pinned by a golden test and exercised by a
  round-trip property, so the R19-F1 class ("the key format changed and
  nobody noticed the rows") now fails loudly at test time.
* `is_ambiguous()` and `decode()` can no longer disagree about what a
  scalar type can emit — the R20-F1 class is closed by construction, not
  by two functions being kept in sync by hand.
* One fewer duplicated fail-closed cookie rule (the R6-F3 class).

### Negative / trade-offs

* The encoded format still has no escaping, so an issuer containing a
  literal `:str:`/`:int:`/`:float:`/`:bool:` marker decodes to an
  ambiguous split. Adding escaping would change persisted bytes, which
  is exactly the migration hazard this ADR refuses to take on for a
  case no real (https-URL) issuer can hit. `decode()`'s re-encode check
  bounds the damage: it can never return a subject that would have been
  stored differently.
* `SsoSubject` accepts `float` subs including `nan`/`inf`, whose value
  equality is odd (`nan != nan`) even though the encoded key is stable.
  Preserved deliberately — narrowing it would be a policy change, and
  reconciliation matches on the encoded string, not on Python equality.
* Only the OIDC path is typed. The proxy-header path's subject is still
  a plain `proxy:<raw-header>` string in its own disjoint namespace; it
  has never had a type tag and needs none (the header value is always a
  `str`). Typing it too would be a second migration for no finding.

## Alternatives considered

* **`json.dumps` canonicalisation of `(iss, sub)`** — considered and
  rejected at R18-F1, for the same reason it is rejected here: it
  changes the persisted bytes for *every* existing row, and the type
  tag already discriminates without a format migration. The typed
  re-expression keeps that property.
* **Keep the free functions, add tests** — the option the previous four
  rounds effectively took. The ledger's own conclusion (pass 2 of the
  architecture review) is that every bug class closed *structurally*
  stopped recurring, and every class closed *by discipline* kept
  recurring. Five rounds is enough evidence.
* **Delete the legacy fallback entirely** — would re-open R19-F1
  (pre-R18-F1 rows orphaned on next login) for the population R19-F1
  was filed over. R20-F1's refuse-on-ambiguity already shrinks the
  fallback to the provably-safe subset; deleting it outright trades a
  real availability bug for no additional safety.
* **A separate `sso_subject.py` module** — the value type is used by one
  module and one flow; a new module would spread the SSO reconciliation
  story across two files for no gain. Revisit if the proxy path is ever
  typed too.

## Verification

* `tests/router/test_sso_subject_value_type.py` (new, 40 tests): the
  golden wire format; construction/acceptance rules; type-exact
  equality; a 600-pair deterministic fuzz for `decode(encode(x)) == x`;
  the totality guard on `decode`; the R18-F1 collision set as a
  parametrised repro; the R19-F1 legacy-reconcile + self-heal repro and
  the R20-F1 takeover-refusal repro, both driven through
  `find_or_create_sso_user` with keys minted by the value type.
* The pre-existing round repros are unchanged in substance and still
  green through the new path — `test_sec_r16_f1_oidc_claim_typeconfusion.py`,
  `test_sec_r19_f1_sso_subject_backcompat.py`,
  `test_sec_r20_f1_sso_legacy_fallback_collision.py` (their unit-level
  calls were re-pointed at the typed API; every assertion is verbatim).
* `test_login_flow.py` / `test_fail_closed_guards.py` cover the
  `_cookie_secure_flag` delegation, including R6-F3's forged-
  `X-Forwarded-Proto` case on the SSO path.
* No live OIDC IdP is involved: this path is inert on the live pentest
  target (BUILTIN auth mode), so the property/unit gate is the real one.
