//! Text chunking for the RAG periodic indexer (Phase F, prancy-napping-
//! pie -- the "6th background loop" found while porting `test_sec_r31_
//! rag_watermark.py`, deliberately deferred at the time and resumed
//! once the operator confirmed nothing else needed doing first).
//! Port of `agent_mcp/features/rag/chunking.py`'s `simple_chunker`/
//! `markdown_aware_chunker` -- the only two chunkers real production
//! ever uses: `AGENT_MCP_EMBEDDING_DIMENSION`/`--advanced` are never
//! set in the real deploy repo, so `code_chunking.py`'s code-aware
//! chunker (entity extraction, per-language summaries, ~584 LOC) has
//! zero real production call site today. Deliberately NOT ported here
//! -- a real, evidence-based deferral (confirmed via the deploy repo's
//! own config, not assumed), matching this migration's own established
//! "confirm the real on/off state before deferring" discipline (Phase
//! D4 decision 4's `ENABLE_TASK_PLACEMENT_RAG` precedent).

/// Fixed-size sliding-window chunking by character count, with overlap.
/// Port of `simple_chunker` -- used for every non-markdown source
/// (`context`) and as `markdown_aware_chunker`'s own emergency
/// fallback never actually needed in practice (see that function's
/// own doc).
///
/// # Panics
/// `chunk_size` must be positive, `overlap` non-negative and strictly
/// less than `chunk_size` -- the same three `ValueError`s Python
/// raises, ported as `assert!` since these are caller-supplied
/// constants, never untrusted input (identical to Python's own
/// docstring not documenting a soft failure mode either).
pub fn simple_chunker(text: &str, chunk_size: usize, overlap: usize) -> Vec<String> {
    assert!(chunk_size > 0, "chunk_size must be a positive integer");
    assert!(overlap < chunk_size, "overlap must be less than chunk_size");
    if text.is_empty() {
        return Vec::new();
    }

    // Python slices by character (str is a sequence of code points);
    // Rust's &str is UTF-8 bytes. Chunk over a Vec<char> so a
    // multi-byte character can never land split across a chunk
    // boundary -- collecting once up front, not on every slice.
    let chars: Vec<char> = text.chars().collect();
    let text_len = chars.len();
    let step = chunk_size - overlap;

    let mut chunks = Vec::new();
    let mut start = 0usize;
    while start < text_len {
        let end = (start + chunk_size).min(text_len);
        chunks.push(chars[start..end].iter().collect());
        start += step;
    }
    chunks
}

/// Structure-aware Markdown chunking: prefers to break on a heading or
/// a new paragraph once the current chunk has reached `min_chunk_size`,
/// carrying `overlap_lines` trailing lines into the next chunk for
/// context. Port of `markdown_aware_chunker`.
///
/// Faithful port of a real, admitted rough edge: Python's own inline
/// comments on the oversized-chunk fallback say outright that a clean
/// 1:1 translation of the *original* (pre-refactor) character-level
/// split was "hard to replicate perfectly" once the function became
/// line-based, and settled for a looser approximation (finalize the
/// chunk built from lines *before* the line that pushed it over
/// `target_chunk_size * 1.5`, but only when that prefix already meets
/// `min_chunk_size` -- otherwise the oversized line is absorbed and the
/// chunk is simply allowed to run long). This port keeps that same
/// looser approximation rather than "fixing" it into something
/// stricter Python never actually shipped.
pub fn markdown_aware_chunker(
    text: &str,
    target_chunk_size: usize,
    min_chunk_size: usize,
    overlap_lines: usize,
) -> Vec<String> {
    assert!(target_chunk_size > 0 && min_chunk_size > 0);
    assert!(min_chunk_size <= target_chunk_size);
    if text.is_empty() {
        return Vec::new();
    }

    let lines: Vec<&str> = text.split('\n').collect();
    let mut chunks: Vec<String> = Vec::new();
    let mut current_chunk_lines: Vec<String> = Vec::new();
    let mut current_chunk_char_count: usize = 0;
    let mut line_buffer_for_overlap: Vec<String> = Vec::new();

    // Sum of each line's char length + 1 (the newline `join` would
    // insert) -- matches Python's own `sum(len(l) + 1 for l in ...) -
    // 1` accounting exactly (the trailing "-1" there cancels the one
    // extra newline this running total would otherwise count for a
    // join with one fewer separator than lines).
    fn char_count(lines: &[String]) -> usize {
        if lines.is_empty() {
            return 0;
        }
        lines.iter().map(|l| l.chars().count() + 1).sum::<usize>() - 1
    }

    for (i, line_content) in lines.iter().enumerate() {
        let is_heading = line_content.trim_start().starts_with('#');
        let is_new_paragraph =
            i > 0 && lines[i - 1].trim().is_empty() && !line_content.trim().is_empty();

        if (is_heading || is_new_paragraph) && current_chunk_char_count >= min_chunk_size {
            if !current_chunk_lines.is_empty() {
                chunks.push(current_chunk_lines.join("\n").trim().to_string());
            }
            current_chunk_lines = line_buffer_for_overlap.clone();
            current_chunk_lines.push(line_content.to_string());
            current_chunk_char_count = char_count(&current_chunk_lines);
        } else {
            current_chunk_lines.push(line_content.to_string());
            current_chunk_char_count += line_content.chars().count() + 1;
        }

        // Oversized-chunk fallback -- see this fn's own doc for why
        // this stays a loose approximation, faithfully ported as-is.
        if current_chunk_char_count > (target_chunk_size as f64 * 1.5) as usize
            && current_chunk_lines.len() > overlap_lines + 1
        {
            let joined_len: usize = current_chunk_lines
                .iter()
                .map(|l| l.chars().count() + 1)
                .sum::<usize>()
                .saturating_sub(1);
            if joined_len > (target_chunk_size as f64 * 1.5) as usize {
                let lines_before_current = &current_chunk_lines[..current_chunk_lines.len() - 1];
                let char_count_before_current = char_count(lines_before_current);
                if char_count_before_current >= min_chunk_size {
                    chunks.push(lines_before_current.join("\n").trim().to_string());
                    current_chunk_lines = line_buffer_for_overlap.clone();
                    current_chunk_lines.push(line_content.to_string());
                    current_chunk_char_count = char_count(&current_chunk_lines);
                }
            }
        }

        line_buffer_for_overlap = if current_chunk_lines.len() > overlap_lines {
            current_chunk_lines[current_chunk_lines.len() - overlap_lines..].to_vec()
        } else {
            current_chunk_lines.clone()
        };
    }

    if !current_chunk_lines.is_empty() {
        let final_chunk = current_chunk_lines.join("\n").trim().to_string();
        if !final_chunk.is_empty() {
            chunks.push(final_chunk);
        }
    }

    chunks.into_iter().filter(|c| !c.is_empty()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- simple_chunker ---------------------------------------------

    #[test]
    fn simple_chunker_empty_text_is_empty() {
        assert_eq!(simple_chunker("", 500, 50), Vec::<String>::new());
    }

    #[test]
    fn simple_chunker_shorter_than_chunk_size_is_one_chunk() {
        assert_eq!(simple_chunker("hello", 500, 50), vec!["hello".to_string()]);
    }

    #[test]
    fn simple_chunker_splits_with_overlap() {
        // 10-char text, chunk_size=4, overlap=1 -> step=3
        let chunks = simple_chunker("0123456789", 4, 1);
        assert_eq!(chunks, vec!["0123", "3456", "6789", "9"]);
    }

    #[test]
    fn simple_chunker_no_overlap_is_exact_partition() {
        let chunks = simple_chunker("abcdefgh", 4, 0);
        assert_eq!(chunks, vec!["abcd", "efgh"]);
    }

    #[test]
    #[should_panic(expected = "chunk_size must be a positive integer")]
    fn simple_chunker_rejects_zero_chunk_size() {
        simple_chunker("x", 0, 0);
    }

    #[test]
    #[should_panic(expected = "overlap must be less than chunk_size")]
    fn simple_chunker_rejects_overlap_ge_chunk_size() {
        simple_chunker("x", 4, 4);
    }

    #[test]
    fn simple_chunker_never_splits_a_multibyte_character() {
        // "é" is 2 UTF-8 bytes but 1 char -- a byte-indexed slice at
        // an odd boundary would panic or corrupt the string.
        let text = "éééé";
        let chunks = simple_chunker(text, 2, 0);
        assert_eq!(chunks, vec!["éé", "éé"]);
        for c in &chunks {
            assert!(c.is_char_boundary(c.len()));
        }
    }

    // -- markdown_aware_chunker --------------------------------------

    #[test]
    fn markdown_chunker_empty_text_is_empty() {
        assert_eq!(
            markdown_aware_chunker("", 1000, 200, 2),
            Vec::<String>::new()
        );
    }

    #[test]
    fn markdown_chunker_short_text_is_one_chunk() {
        let text = "# Title\n\nA short paragraph.";
        let chunks = markdown_aware_chunker(text, 1000, 200, 2);
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0], text);
    }

    // The next two cases' expected output was captured by running the
    // REAL Python `markdown_aware_chunker` against the identical input
    // (not hand-derived) -- this function's own oversized-chunk
    // fallback interacts with the overlap-line carry-forward in a way
    // that produces a real, confirmed content duplication across
    // chunk boundaries (the same trailing lines can be captured into
    // `line_buffer_for_overlap` more than once before the split that
    // consumes them actually fires). Ported as-is, not "fixed" --
    // matching this migration's own "port documented behavior, don't
    // silently reconcile" discipline.
    #[test]
    fn markdown_chunker_splits_on_heading_past_min_size() {
        let para = "x".repeat(250);
        let text = format!("# First\n\n{para}\n\n# Second\n\nshort tail");
        let chunks = markdown_aware_chunker(&text, 1000, 200, 2);
        assert_eq!(chunks.len(), 3);
        assert_eq!(chunks[0], format!("# First\n\n{para}"));
        assert_eq!(chunks[1], format!("{para}\n\n# Second"));
        assert_eq!(chunks[2], "# Second\n\nshort tail");
    }

    #[test]
    fn markdown_chunker_never_produces_empty_chunks() {
        let text = "\n\n\n# H\n\n\n";
        let chunks = markdown_aware_chunker(text, 1000, 200, 2);
        assert!(chunks.iter().all(|c| !c.is_empty()));
    }

    #[test]
    fn markdown_chunker_overlap_lines_carry_into_next_chunk() {
        let para = "y".repeat(250);
        let text = format!("context line\n\n# Section\n\n{para}\n\n# Next\n\ntail");
        let chunks = markdown_aware_chunker(&text, 1000, 200, 1);
        assert_eq!(chunks.len(), 2);
        assert_eq!(chunks[0], format!("context line\n\n# Section\n\n{para}"));
        // The one-line overlap carried here is a blank line, which
        // `.strip()`/`.trim()` removes entirely -- the real Python
        // output genuinely has nothing visible prepended in this
        // specific case (confirmed against real output, not assumed).
        assert_eq!(chunks[1], "# Next\n\ntail");
    }

    /// Golden test: a realistic multi-section document, expected output
    /// captured by running the real Python function directly (not
    /// hand-derived). Confirms the overlap feature's real, intended
    /// shape -- each section's last line genuinely appears at both the
    /// END of its own chunk and the START of the next one, giving the
    /// next chunk one line of trailing context -- is preserved exactly.
    #[test]
    fn markdown_chunker_realistic_document_matches_python_golden_output() {
        let text = "# Introduction\n\nThis is the introduction paragraph explaining what this document covers in reasonable detail so it has some real length to it.\n\n## Background\n\nHere is some background information that spans a couple of sentences to give context.\n\n## Details\n\nMore detailed content goes here, with enough text to potentially trigger chunking behavior depending on the target size chosen for this particular test case scenario.\n\n# Conclusion\n\nWrapping up the document with a final paragraph.\n";
        let chunks = markdown_aware_chunker(text, 200, 50, 2);
        let expected = vec![
            "# Introduction\n\nThis is the introduction paragraph explaining what this document covers in reasonable detail so it has some real length to it.",
            "This is the introduction paragraph explaining what this document covers in reasonable detail so it has some real length to it.\n\n## Background",
            "## Background\n\nHere is some background information that spans a couple of sentences to give context.",
            "Here is some background information that spans a couple of sentences to give context.\n\n## Details",
            "## Details\n\nMore detailed content goes here, with enough text to potentially trigger chunking behavior depending on the target size chosen for this particular test case scenario.",
            "More detailed content goes here, with enough text to potentially trigger chunking behavior depending on the target size chosen for this particular test case scenario.\n\n# Conclusion",
            "# Conclusion\n\nWrapping up the document with a final paragraph.",
        ];
        assert_eq!(chunks, expected);
    }
}
