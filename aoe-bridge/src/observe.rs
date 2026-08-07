//! Leveled, redacted, durable diagnostics.
//!
//! ## Why a file and not just stderr
//!
//! The AoE plugin host redirects a runtime worker's stderr into
//! `<app_dir>/plugin-workers/<uuid-v4>.log` (`src/plugin/host.rs`,
//! `PluginHost::spawn_once`). That file is real, but it is:
//! - named after a **fresh random UUID generated per spawn**, so its path is
//!   unguessable and changes on every respawn;
//! - never read by anything — no tail endpoint, no ring buffer, no rotation, no
//!   `aoe logs` integration, no UI surface (`workers_dir` has exactly one use in
//!   the host: creating the file);
//! - not routed to the journal, so `journalctl -u aoe-web.service | grep
//!   aoe-bridge` returns nothing.
//!
//! The host offers **no logging RPC at all** — the worker's entire operator-
//! facing surface is `ui.state.*` and `ui.notify`, both of which carry *state*,
//! not *history*. So the bridge writes its own durable log at a **deterministic,
//! discoverable path**, and publishes that path in its settings page ([`crate::status`])
//! so an operator can find it from the UI rather than guessing.
//!
//! stderr is still written (it costs nothing and a host that ever grows a log
//! surface picks it up for free); the file is the channel an operator can
//! actually `tail`.
//!
//! ## Redaction
//!
//! Every message — stderr and file alike — passes through [`redact`] before it
//! is emitted. The bridge's settings carry a per-session agent-mcp bearer and an
//! AoE serve bearer, and those tokens end up in `Authorization` headers that a
//! transport error (or a careless future `format!`) could echo back. Redaction
//! at the single write path is the only place that cannot be forgotten.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

/// Rotate once the live log passes this size, keeping a single `.1` generation.
/// 2 MiB holds roughly a day of `debug` traffic for a handful of sessions, and
/// two generations bound the bridge's disk use at 4 MiB — small enough to leave
/// on by default, large enough to still hold the run that broke.
const MAX_LOG_BYTES: u64 = 2 * 1024 * 1024;

/// Diagnostic severity. Ordered: a message is emitted when its level is at most
/// the configured threshold, so `Error` always survives and `Debug` is opt-in.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Error = 0,
    Warn = 1,
    Info = 2,
    Debug = 3,
}

impl Level {
    pub fn as_str(self) -> &'static str {
        match self {
            Level::Error => "error",
            Level::Warn => "warn",
            Level::Info => "info",
            Level::Debug => "debug",
        }
    }

    /// Parse a level name, case-insensitively. Unknown or empty ⇒ `None`, so the
    /// caller keeps its default rather than silently going quiet.
    pub fn parse(s: &str) -> Option<Level> {
        match s.trim().to_ascii_lowercase().as_str() {
            "error" => Some(Level::Error),
            "warn" | "warning" => Some(Level::Warn),
            "info" => Some(Level::Info),
            "debug" | "trace" => Some(Level::Debug),
            _ => None,
        }
    }
}

/// The process-wide sink: the threshold plus the (optional) durable file.
struct Sink {
    file: Option<Mutex<File>>,
    path: Option<PathBuf>,
}

static SINK: OnceLock<Sink> = OnceLock::new();
/// Threshold, stored separately from `SINK` so [`enabled`] is a plain atomic
/// read on the hot path (one per frame injected).
static THRESHOLD: AtomicU8 = AtomicU8::new(Level::Info as u8);

/// Resolve the durable log path from the environment.
///
/// - `AOE_BRIDGE_LOG_FILE` set and non-empty ⇒ that path.
/// - `AOE_BRIDGE_LOG_FILE` set and **empty** ⇒ `None` (file logging off; an
///   explicit opt-out for an operator who only wants stderr).
/// - unset ⇒ `$XDG_STATE_HOME/aoe-bridge/worker.log`, falling back to
///   `$HOME/.local/state/aoe-bridge/worker.log` per the XDG base-dir spec (state
///   is the right category: logs are persistent-but-not-precious data).
pub fn resolve_log_path(
    log_file: Option<&str>,
    xdg_state_home: Option<&str>,
    home: Option<&str>,
) -> Option<PathBuf> {
    if let Some(explicit) = log_file {
        let trimmed = explicit.trim();
        return if trimmed.is_empty() {
            None
        } else {
            Some(PathBuf::from(trimmed))
        };
    }
    let base = match xdg_state_home.map(str::trim).filter(|s| !s.is_empty()) {
        Some(state) => PathBuf::from(state),
        None => PathBuf::from(home?.trim()).join(".local/state"),
    };
    Some(base.join("aoe-bridge").join("worker.log"))
}

/// Install the process-wide sink. Idempotent; the first call wins.
///
/// Failing to open the log file is **not** fatal — the worker still runs and
/// still writes stderr. It reports the failure on stderr so the degradation is
/// visible rather than silent.
pub fn init() {
    let level = std::env::var("AOE_BRIDGE_LOG_LEVEL")
        .ok()
        .as_deref()
        .and_then(Level::parse)
        .unwrap_or(Level::Info);
    THRESHOLD.store(level as u8, Ordering::Relaxed);

    let path = resolve_log_path(
        std::env::var("AOE_BRIDGE_LOG_FILE").ok().as_deref(),
        std::env::var("XDG_STATE_HOME").ok().as_deref(),
        std::env::var("HOME").ok().as_deref(),
    );

    // A path that failed to open is dropped from the sink entirely, so
    // `log_path()` never advertises a file the operator cannot tail.
    let opened = path.as_ref().and_then(|p| match open_log(p) {
        Ok(f) => Some(Mutex::new(f)),
        Err(e) => {
            eprintln!("[aoe-bridge] cannot open log file {}: {e}", p.display());
            None
        }
    });
    let _ = SINK.set(Sink {
        path: path.filter(|_| opened.is_some()),
        file: opened,
    });
}

fn open_log(path: &PathBuf) -> std::io::Result<File> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    OpenOptions::new().create(true).append(true).open(path)
}

/// The durable log path, for publishing in the settings page. `None` when file
/// logging is off or the file could not be opened.
pub fn log_path() -> Option<String> {
    SINK.get()?.path.as_ref().map(|p| p.display().to_string())
}

/// The active threshold, for publishing in the settings page.
pub fn level() -> Level {
    match THRESHOLD.load(Ordering::Relaxed) {
        0 => Level::Error,
        1 => Level::Warn,
        2 => Level::Info,
        _ => Level::Debug,
    }
}

/// Whether a message at `level` would be emitted. Callers use this to skip
/// building an expensive message on the hot path.
pub fn enabled(level: Level) -> bool {
    (level as u8) <= THRESHOLD.load(Ordering::Relaxed)
}

/// Emit one diagnostic line to stderr and (if configured) the durable file.
/// The message is redacted first — see the module docs.
pub fn log_at(level: Level, msg: &str) {
    if !enabled(level) {
        return;
    }
    let line = format!(
        "{} {:>5} [aoe-bridge] {}",
        iso8601(now_secs()),
        level.as_str(),
        redact(msg)
    );
    eprintln!("{line}");
    if let Some(sink) = SINK.get() {
        if let Some(file) = sink.file.as_ref() {
            if let Ok(mut f) = file.lock() {
                // Rotate before the write so the live file never exceeds the cap
                // by more than one line.
                if let Some(path) = sink.path.as_ref() {
                    rotate_if_needed(&mut f, path);
                }
                let _ = writeln!(f, "{line}");
                let _ = f.flush();
            }
        }
    }
}

/// Swap the live log for its `.1` generation once it passes [`MAX_LOG_BYTES`].
/// Best-effort: a failed rename or reopen leaves the existing handle in place,
/// so logging degrades to "grows past the cap" rather than stopping.
fn rotate_if_needed(file: &mut File, path: &PathBuf) {
    let too_big = file
        .metadata()
        .map(|m| m.len() >= MAX_LOG_BYTES)
        .unwrap_or(false);
    if !too_big {
        return;
    }
    let rotated = path.with_extension("log.1");
    if std::fs::rename(path, &rotated).is_err() {
        return;
    }
    if let Ok(fresh) = open_log(path) {
        *file = fresh;
    }
}

pub fn error(msg: &str) {
    log_at(Level::Error, msg);
}
pub fn warn(msg: &str) {
    log_at(Level::Warn, msg);
}
pub fn info(msg: &str) {
    log_at(Level::Info, msg);
}
pub fn debug(msg: &str) {
    log_at(Level::Debug, msg);
}

// ---------------------------------------------------------------------------
// Redaction
// ---------------------------------------------------------------------------

/// Secret-bearing prefixes. A run of value characters following any of these
/// (case-insensitively) is replaced with `***`.
const SECRET_KEYS: &[&str] = &[
    "token=",
    "access_token=",
    "api_key=",
    "apikey=",
    "secret=",
    "password=",
];

/// Scrub bearer tokens and secret-shaped query parameters out of a diagnostic.
///
/// Two rules, both case-insensitive:
/// - `Bearer <run-of-non-whitespace>` ⇒ `Bearer ***`
/// - `<key>=<run>` for the keys in [`SECRET_KEYS`] ⇒ `<key>***`, where the run
///   ends at whitespace or any of `& " ' , ) }` — the delimiters a token can
///   plausibly abut in a URL, a JSON fragment, or a `Debug` rendering.
///
/// Deliberately conservative: over-redacting a diagnostic costs a little
/// clarity, under-redacting one leaks a credential into a file on disk.
pub fn redact(msg: &str) -> String {
    let lower = msg.to_ascii_lowercase();
    let bytes = msg.as_bytes();
    let mut out = String::with_capacity(msg.len());
    let mut i = 0usize;
    'outer: while i < bytes.len() {
        if lower[i..].starts_with("bearer ") {
            out.push_str(&msg[i..i + 7]);
            i += 7;
            i = skip_secret(msg, i, &mut out, |c| c.is_whitespace());
            continue;
        }
        for key in SECRET_KEYS {
            if lower[i..].starts_with(key) {
                out.push_str(&msg[i..i + key.len()]);
                i += key.len();
                i = skip_secret(msg, i, &mut out, |c| {
                    c.is_whitespace() || matches!(c, '&' | '"' | '\'' | ',' | ')' | '}')
                });
                continue 'outer;
            }
        }
        // Advance one whole char so multi-byte UTF-8 is never split.
        let ch = msg[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

/// Consume the secret run starting at `i`, pushing `***` in its place. Returns
/// the index just past the run. An empty run (the key is present but the value
/// is missing) emits nothing, so `token=` stays `token=`.
fn skip_secret(msg: &str, i: usize, out: &mut String, is_end: impl Fn(char) -> bool) -> usize {
    let mut j = i;
    for ch in msg[i..].chars() {
        if is_end(ch) {
            break;
        }
        j += ch.len_utf8();
    }
    if j > i {
        out.push_str("***");
    }
    j
}

// ---------------------------------------------------------------------------
// Time
// ---------------------------------------------------------------------------

/// Seconds since the Unix epoch. Saturates to 0 if the clock is before 1970,
/// which only a badly-set clock produces and which must not panic a worker.
pub fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Render epoch seconds as a UTC RFC-3339 timestamp (`2026-08-07T09:14:03Z`).
///
/// Hand-rolled rather than pulling a date crate into a single-purpose plugin
/// binary: this is Howard Hinnant's `civil_from_days`, the standard proleptic-
/// Gregorian conversion, and it is covered by tests against known epochs.
pub fn iso8601(epoch_secs: u64) -> String {
    let days = (epoch_secs / 86_400) as i64;
    let rem = epoch_secs % 86_400;
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (y + i64::from(m <= 2), m, d)
}

/// Render a duration as a compact human age (`just now`, `42s`, `7m`, `3h`,
/// `2d`). Used by the settings page, where "last inject 3h ago" reads far
/// faster than an absolute timestamp.
pub fn format_age(secs: u64) -> String {
    match secs {
        0..=1 => "just now".to_string(),
        2..=59 => format!("{secs}s ago"),
        60..=3599 => format!("{}m ago", secs / 60),
        3600..=86_399 => format!("{}h ago", secs / 3600),
        _ => format!("{}d ago", secs / 86_400),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn levels_order_error_first() {
        assert!(Level::Error < Level::Warn);
        assert!(Level::Warn < Level::Info);
        assert!(Level::Info < Level::Debug);
    }

    #[test]
    fn level_parse_is_case_insensitive_and_defaults_to_none() {
        assert_eq!(Level::parse("DEBUG"), Some(Level::Debug));
        assert_eq!(Level::parse(" Warning "), Some(Level::Warn));
        assert_eq!(Level::parse("info"), Some(Level::Info));
        assert_eq!(Level::parse(""), None);
        assert_eq!(Level::parse("chatty"), None);
    }

    #[test]
    fn redacts_bearer_tokens() {
        assert_eq!(
            redact("inject failed: Authorization: Bearer sk-abc123def"),
            "inject failed: Authorization: Bearer ***"
        );
        // Case-insensitive on the scheme, and the rest of the line survives.
        assert_eq!(
            redact("header bearer TOP_SECRET sent to host"),
            "header bearer *** sent to host"
        );
    }

    #[test]
    fn redacts_secret_query_parameters() {
        assert_eq!(
            redact("GET https://mcp/api/p/delivery/stream?token=abc123&x=1 failed"),
            "GET https://mcp/api/p/delivery/stream?token=***&x=1 failed"
        );
        assert_eq!(redact(r#"{"api_key=hunter2"}"#), r#"{"api_key=***"}"#);
    }

    #[test]
    fn redaction_leaves_ordinary_text_untouched() {
        let msg = "inject to sid-1 returned HTTP 400 acp_mode_unsupported";
        assert_eq!(redact(msg), msg);
        // A key with no value is left alone rather than gaining a bogus ***.
        assert_eq!(redact("token="), "token=");
    }

    #[test]
    fn redaction_is_utf8_safe() {
        // Multi-byte characters must not be split by the byte-indexed scanner.
        assert_eq!(redact("stream für sid-é ok"), "stream für sid-é ok");
        assert_eq!(redact("é Bearer tok é"), "é Bearer *** é");
    }

    #[test]
    fn log_path_prefers_explicit_env_and_honours_empty_optout() {
        assert_eq!(
            resolve_log_path(Some("/var/log/bridge.log"), None, None),
            Some(PathBuf::from("/var/log/bridge.log"))
        );
        assert_eq!(resolve_log_path(Some("  "), Some("/s"), Some("/h")), None);
    }

    #[test]
    fn log_path_falls_back_through_xdg_then_home() {
        assert_eq!(
            resolve_log_path(None, Some("/s"), Some("/h")),
            Some(PathBuf::from("/s/aoe-bridge/worker.log"))
        );
        assert_eq!(
            resolve_log_path(None, None, Some("/h")),
            Some(PathBuf::from("/h/.local/state/aoe-bridge/worker.log"))
        );
        // No HOME and no XDG_STATE_HOME ⇒ no file, rather than a relative path
        // written into whatever cwd the host happened to spawn us in.
        assert_eq!(resolve_log_path(None, None, None), None);
    }

    #[test]
    fn iso8601_matches_known_epochs() {
        assert_eq!(iso8601(0), "1970-01-01T00:00:00Z");
        assert_eq!(iso8601(1_000_000_000), "2001-09-09T01:46:40Z");
        // A leap day, which is where a hand-rolled calendar goes wrong.
        assert_eq!(iso8601(1_709_164_800), "2024-02-29T00:00:00Z");
        assert_eq!(iso8601(1_754_524_800), "2025-08-07T00:00:00Z");
    }

    #[test]
    fn format_age_buckets() {
        assert_eq!(format_age(0), "just now");
        assert_eq!(format_age(45), "45s ago");
        assert_eq!(format_age(60), "1m ago");
        assert_eq!(format_age(3_600), "1h ago");
        assert_eq!(format_age(172_800), "2d ago");
    }
}
