//! Minijinja rendering for the login/setup-wizard HTML surface. Port
//! target: `agent_mcp/router/login.py`'s `_render`/`_jinja_env` +
//! the real Jinja2 templates at `agent_mcp/router/templates/`
//! (Phase E2 PR23 step 4, `conexus-router-login-setup-templates`).
//!
//! Operator decision 2026-09-06 (`prancy-napping-pie`): minijinja
//! over askama or hand-written `format!` HTML. Askama's template
//! syntax is Rust-native and different from Jinja2's -- porting to it
//! would mean REWRITING the 3 template files below, not porting
//! them. minijinja is a Jinja2-syntax-compatible reimplementation (by
//! Jinja2's own author, Armin Ronacher), so `{% extends %}`/
//! `{% block %}` inheritance and every `{{ }}` interpolation below
//! carry over near-verbatim. Hand-written `format!` HTML was rejected
//! because it makes autoescaping a per-editor discipline instead of a
//! structural property -- exactly the bug class (a future field added
//! without remembering to escape it) this project's own pentest
//! history keeps finding.
//!
//! `include_str!`'d directly from the canonical Python location
//! rather than duplicated into this crate -- one source of truth,
//! the identical, already-established
//! `conexus_tools::prompts::CATALOG_JSON` pattern (a deliberate,
//! temporary cross-language coupling per the migration plan, retired
//! only in Phase F). **Not yet exercised by any Nix packaging**: no
//! `conexusRouter` derivation exists in `nix/conexus.nix` yet (only
//! `conexusBackend` does, scoped via `cargoExtraArgs = "-p
//! conexus-backend"`) -- whoever adds one will need the identical
//! `postUnpack` copy-in fix PR #850 added for `conexus-backend`'s own
//! `catalog.json` embed, or this same class of Nix-sandbox gap
//! recurs.
//!
//! **Confirmed, not assumed**: minijinja's HTML autoescape is
//! STRICTER than Python markupsafe's -- it additionally escapes `/`
//! to `&#x2f;` (an OWASP-recommended defense-in-depth choice;
//! markupsafe only escapes `&`/`<`/`>`/`"`/`'`). Every `{{ }}`
//! interpolation in these templates renders a path/URL/username, so
//! this surfaces as extra `&#x2f;` entities in the byte-for-byte
//! output vs. Python's -- still correct, safe HTML (a browser decodes
//! the entity back to `/` when parsing an attribute value), verified
//! live against the real renderer (see this module's own tests)
//! before being noted here, not inferred from minijinja's docs alone.
#![allow(dead_code)]

use std::sync::LazyLock;

use minijinja::{context, AutoEscape, Environment};

const BASE_HTML: &str = include_str!("../../../agent_mcp/router/templates/base.html");
const LOGIN_HTML: &str = include_str!("../../../agent_mcp/router/templates/login.html");
const SETUP_HTML: &str = include_str!("../../../agent_mcp/router/templates/setup.html");

static ENV: LazyLock<Environment<'static>> = LazyLock::new(|| {
    let mut env = Environment::new();
    // Port of Python's `autoescape=select_autoescape(["html", "xml"])`
    // -- every template here is `.html`, so this is unconditionally
    // HTML-autoescape, matching the real Environment's own effective
    // behavior for these 3 templates exactly (no `.txt`/other
    // extension is ever loaded through this Environment).
    env.set_auto_escape_callback(|_name| AutoEscape::Html);
    env.add_template("base.html", BASE_HTML).expect(
        "base.html is a fixed, developer-authored template -- a parse failure is a build-time bug",
    );
    env.add_template("login.html", LOGIN_HTML).expect(
        "login.html is a fixed, developer-authored template -- a parse failure is a build-time bug",
    );
    env.add_template("setup.html", SETUP_HTML).expect(
        "setup.html is a fixed, developer-authored template -- a parse failure is a build-time bug",
    );
    env
});

/// Port of `login_get_handler`/`login_post_handler`'s `_render("login.html", ...)`
/// call sites. `sso_provider_name` is real for `login_get_handler`'s
/// success render (`sso::resolve_sso_provider_name`, wired in Phase
/// E2 PR22 step 2) -- Python's own real asymmetry preserved: the POST
/// error-rerender paths never resolve it (an omitted kwarg is falsy
/// in Jinja2's default `Undefined`, matching this struct's own
/// `None` at those call sites exactly), so it stays `None` there, not
/// a bug. `sso_login_url` is always a real, mount-aware URL even in
/// legacy-form mode -- the template's own `{% if sso_provider_name %}`
/// branch is what actually gates whether the button renders.
pub struct LoginPageContext<'a> {
    pub error: Option<&'a str>,
    pub username: &'a str,
    pub next: &'a str,
    pub sso_provider_name: Option<&'a str>,
    pub login_action: &'a str,
    pub sso_login_url: &'a str,
}

pub fn render_login(ctx: &LoginPageContext) -> String {
    ENV.get_template("login.html")
        .expect("login.html registered at Environment construction")
        .render(context! {
            error => ctx.error,
            username => ctx.username,
            next => ctx.next,
            sso_provider_name => ctx.sso_provider_name,
            login_action => ctx.login_action,
            sso_login_url => ctx.sso_login_url,
        })
        .expect("rendering login.html with a valid context cannot fail -- every value is a plain string minijinja autoescapes, never a template-syntax input")
}

/// Port of `_render_setup_form`'s `_render("setup.html", ...)` call.
pub struct SetupPageContext<'a> {
    pub error: Option<&'a str>,
    pub username: &'a str,
    pub email: &'a str,
}

pub fn render_setup(ctx: &SetupPageContext) -> String {
    ENV.get_template("setup.html")
        .expect("setup.html registered at Environment construction")
        .render(context! {
            error => ctx.error,
            username => ctx.username,
            email => ctx.email,
        })
        .expect("rendering setup.html with a valid context cannot fail -- every value is a plain string minijinja autoescapes, never a template-syntax input")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_login_produces_the_legacy_form_with_no_sso_provider() {
        let html = render_login(&LoginPageContext {
            error: None,
            username: "",
            next: "",
            sso_provider_name: None,
            login_action: "/agent-mcp/login",
            sso_login_url: "/agent-mcp/sso/login",
        });
        // minijinja's HTML autoescape escapes `/` to `&#x2f;` (an
        // OWASP-recommended, defense-in-depth escaper choice stricter
        // than Python markupsafe's, which leaves `/` alone) --
        // confirmed live against the real renderer before writing this
        // assertion, not assumed byte-identical to Python. Still
        // correct, safe HTML: a browser decodes the entity back to `/`
        // when parsing the attribute value.
        assert!(html.contains(r#"<form method="post" action="&#x2f;agent-mcp&#x2f;login">"#));
        assert!(html.contains("name=\"username\""));
        assert!(html.contains("name=\"password\""));
        assert!(!html.contains("Sign in via your organisation"));
    }

    #[test]
    fn render_login_escapes_an_error_message_and_username() {
        let html = render_login(&LoginPageContext {
            error: Some("<script>alert(1)</script>"),
            username: "<b>alice</b>",
            next: "/app/",
            sso_provider_name: None,
            login_action: "/agent-mcp/login",
            sso_login_url: "/agent-mcp/sso/login",
        });
        assert!(!html.contains("<script>alert(1)</script>"));
        assert!(html.contains("&lt;script&gt;alert(1)&lt;&#x2f;script&gt;"));
        assert!(!html.contains("value=\"<b>alice</b>\""));
        assert!(html.contains("&lt;b&gt;alice&lt;&#x2f;b&gt;"));
    }

    #[test]
    fn render_login_shows_the_sso_button_when_a_provider_name_is_given() {
        let html = render_login(&LoginPageContext {
            error: None,
            username: "",
            next: "",
            sso_provider_name: Some("Example IdP"),
            login_action: "/agent-mcp/login",
            sso_login_url: "/agent-mcp/sso/login",
        });
        assert!(html.contains("Sign in with"));
        assert!(html.contains("Example IdP"));
        assert!(!html.contains("name=\"password\""));
    }

    #[test]
    fn render_login_appends_the_next_query_param_to_the_form_action() {
        let html = render_login(&LoginPageContext {
            error: None,
            username: "",
            next: "/app/foo/",
            sso_provider_name: None,
            login_action: "/agent-mcp/login",
            sso_login_url: "/agent-mcp/sso/login",
        });
        assert!(
            html.contains("action=\"&#x2f;agent-mcp&#x2f;login?next=&#x2f;app&#x2f;foo&#x2f;\"")
        );
    }

    #[test]
    fn render_setup_produces_the_bootstrap_form() {
        let html = render_setup(&SetupPageContext {
            error: None,
            username: "",
            email: "",
        });
        assert!(html.contains("Create the first operator"));
        assert!(html.contains("name=\"username\""));
        assert!(html.contains("name=\"password_confirm\""));
    }

    #[test]
    fn render_setup_escapes_error_username_and_email() {
        let html = render_setup(&SetupPageContext {
            error: Some("Username is required."),
            username: "\"><script>bad</script>",
            email: "a@b.test",
        });
        assert!(html.contains("Username is required."));
        assert!(!html.contains("\"><script>bad</script>"));
        assert!(html.contains("a@b.test"));
    }
}
