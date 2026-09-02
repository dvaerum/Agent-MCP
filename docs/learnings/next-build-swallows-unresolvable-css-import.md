# A dead CSS `@import` doesn't fail `next build` — it empties the whole bundle

Found live in production 2026-09-02. PR #759 removed the `vis-network`
npm dependency and its component files, but missed a separate
`@import "vis-network/styles/vis-network.css";` in `app/globals.css`
(a global import, unrelated to the component tree — the grep sweep
before shipping only scanned `.tsx`/`.ts`/`.json`, not `.css`, so it
never surfaced this).

The dangerous part: `next build` did **not** fail.

```
[Error: Can't resolve 'vis-network/styles/vis-network.css' in '.../app']
 ✓ Compiled successfully in 7.2s
   ...
 ✓ Exporting (2/2)
```

Webpack prints the resolve failure as diagnostic output, but treats it
as non-fatal for the CSS pipeline specifically — the build still exits
0, still prints "Compiled successfully," and still emits a
`_next/static/css/<hash>.css` file... except that file is exactly
**0 bytes**. Every page on the live dashboard rendered as unstyled
plain HTML for every user, including the public internet-facing
deployment, and nothing caught it: `tsc --noEmit` doesn't run a real
build, `vitest run` doesn't render `globals.css`, and CI's "Dashboard
build" job apparently didn't diff or size-check the output artifact.

**Fix**: delete the dead `@import`. **Regression guard**:
`tests/test_dashboard_css_imports_resolve.py` parses every
`@import "pkg/...";` in `globals.css` and asserts the package name is
declared in `package.json`.

**When this recurs**: any time a dependency is removed, grep for it
across `.css` files too, not just `.ts`/`.tsx` — the frontend build's
own error output for this class of bug looks like a passing build at
a glance, so a source-level regression test is the real backstop, not
"the build didn't error." Worth escalating further if it recurs: check
whether `next.config`'s webpack config can be told to treat CSS
`@import` resolve failures as fatal, rather than relying solely on the
grep-based guard.
