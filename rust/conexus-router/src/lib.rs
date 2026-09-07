//! Library target for `conexus-router`, alongside its `conexus-router`
//! binary target (same crate, both compiled from the same source
//! tree -- a standard bin+lib package shape). Exists ONLY so a
//! separate binary (`conexus-cli`) can reuse a couple of genuinely
//! self-contained modules (`identity`, `project_registry` -- both
//! confirmed zero-dependency on the rest of this crate, no `use
//! crate::` anywhere in either file) without depending on the whole
//! router binary or duplicating security-sensitive password-hashing
//! logic. Every other module stays declared only in `main.rs` --
//! this is deliberately NOT "make the whole router a library",
//! matching this migration's own "don't over-scope a refactor beyond
//! what a real caller needs" discipline.
pub mod identity;
pub mod project_registry;
