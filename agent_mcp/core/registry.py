"""Unified `Registry[T]` for named, role-gated, dispatchable entries.

Candidate B from the 2026-06-02 architecture review. Before this,
three subsystems — tools, resources, prompts — each invented their
own "register a named thing and dispatch it" shape:

* **Tools** had a dict-of-dicts (`tool_schemas` + `tool_implementations`)
  plus a hand-maintained classification table at
  `agent_mcp/tools/access.py::TOOL_ACCESS` for `tools/list` filtering.
* **Resources** routed by URI prefix with auth checks inlined into
  each handler.
* **Prompts** were a static JSON catalog with no auth and no
  visibility filter at all — a worker calling `prompts/list` saw the
  full catalogue even for admin-only operational prompts.

Adding a fourth surface would have meant inventing a fourth shape;
adding visibility to prompts would have meant re-implementing what
`tools/access.py` already does. Hence this module: a single,
T-parametric registry whose three subclasses (`ToolRegistry`,
`ResourceRegistry`, `PromptRegistry`) supply only the dispatch verb
(`dispatch` / `read` / `render`), reusing the shared
`register` / `list_visible` / `get` core.

Visibility model
----------------

`RegistryEntry.visibility` is one of:

* `"any"`   — visible to admin, worker, and anonymous callers alike.
* `"admin"` — visible only to admin.
* A callable `(role: str) -> bool` — invoked per-role to decide
  visibility. Admin is *always* True (admin bypasses every filter);
  anonymous only sees entries whose callable returns True without
  any extra context. Used by tools' `worker-if-toggled:<key>`
  semantics, where the callable reads the project_context toggle
  and decides whether the worker sees the tool.

The role string follows the same vocabulary the rest of the codebase
uses: `"admin"`, `"worker"`, `"anonymous"`. Unknown roles are
treated like anonymous.

Why `T`?
--------

Each subsystem's payload is shaped differently:

* Tools carry `(input_schema, implementation_func)`.
* Resources carry `(uri_prefix, reader_func)`.
* Prompts carry the catalog dict.

Storing them through a generic `meta: T` keeps the shared core free
of subsystem-specific knowledge while letting each subclass type its
own attribute access correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Literal,
    Optional,
    TypeVar,
    Union,
)

from .config import logger

T = TypeVar("T")

#: Visibility sentinel or policy callable. See module docstring.
Visibility = Union[Literal["any", "admin"], Callable[[str], bool]]


def resolve_visibility(visibility: Visibility, role: str) -> bool:
    """Decide whether a caller with `role` should see an entry with
    the given `visibility` declaration.

    * Admin always sees everything.
    * `"any"` is visible to every role.
    * `"admin"` is visible only to admin (caught by the admin
      bypass above).
    * Callable `visibility(role)` decides for non-admin roles. Errors
      in the callable default to *hidden* (defensive: a buggy policy
      should not silently leak entries to workers).
    """
    if role == "admin":
        return True
    if visibility == "any":
        return True
    if visibility == "admin":
        return False
    if callable(visibility):
        try:
            return bool(visibility(role))
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(
                "registry: visibility policy raised %r for role=%r; "
                "treating as hidden.",
                e,
                role,
            )
            return False
    # Unknown sentinel — log loud, hide conservative.
    logger.warning(
        "registry: unknown visibility %r; treating as hidden.", visibility
    )
    return False


@dataclass
class RegistryEntry(Generic[T]):
    """One named entry in a `Registry[T]`.

    `name` is the dispatch key (tool name, resource short-name,
    prompt id). `visibility` decides which roles see the entry in
    `list_visible`. `meta` is the subsystem-specific payload —
    callers typed against `Registry[ToolImpl]` (etc.) get strongly
    typed access to `entry.meta`.
    """

    name: str
    visibility: Visibility
    meta: T


class Registry(Generic[T]):
    """Name → RegistryEntry[T] map with role-based visibility filtering.

    Subsystems subclass this to add their dispatch verb (e.g.
    `dispatch` for tools, `read` for resources, `render` for
    prompts). The base class is purely a container — it does not
    invoke the meta payload itself.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, RegistryEntry[T]] = {}

    def register(self, entry: RegistryEntry[T]) -> None:
        """Insert (or overwrite) a named entry.

        Re-registering an existing name logs a warning and overwrites,
        matching the existing `tools.registry.register_tool` behavior.
        Tests rely on this idempotent-ish shape during reloads.
        """
        if entry.name in self._entries:
            logger.warning(
                "Registry: overwriting existing entry %r", entry.name
            )
        self._entries[entry.name] = entry

    def get(self, name: str) -> Optional[RegistryEntry[T]]:
        """Return the entry for `name`, or None if absent."""
        return self._entries.get(name)

    def list_visible(self, role: str) -> List[RegistryEntry[T]]:
        """Return every entry visible to `role`, in registration order."""
        return [
            entry
            for entry in self._entries.values()
            if resolve_visibility(entry.visibility, role)
        ]

    def names(self) -> Iterable[str]:
        """All registered names (regardless of visibility). Useful
        for invariant tests that compare against the full catalogue.
        """
        return tuple(self._entries.keys())

    def clear(self) -> None:
        """Drop every entry. Used by test helpers that rebuild the
        catalogue from a temporary JSON; production code never calls
        this on the live registries.
        """
        self._entries.clear()
