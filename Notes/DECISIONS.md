# Anki Proteus — Decisions

Design choices for this repo, chronological. Primary sources and synthesis notes
live in `anki-proteus-knowledge/` (gitignored) so the public repo stays lean.

## D1: Repo made Obsidian-aware; knowledge vault split public/private — 2026-04-23

**Problem:** README citations gestured at specific sources ("minimum information
principle," "contextualized recall," "dual coding theory"), but the actual reading
trail wasn't documented anywhere. Future maintenance would lose the grounding; a
reader checking a citation couldn't see the original material, and some
citations risked being performative rather than grounded.

**Decision:** Adopt a lightweight Obsidian-aware structure mirroring the pattern
in `~/Documents/obs_researchDB/OBS_ResearchDB/`:

- `anki-proteus-knowledge/` (gitignored) holds `Raw/` (primary sources, `.md`
  paired with `.pdf` where available), `Clippings/` (web material), `Wiki/`
  (synthesis), `Logs/` (session log).
- `Notes/DECISIONS.md` (this file) is checked in and public — it records the
  design choices this repo is committing to.
- `.gitignore` adds `.obsidian/` and `.trash/` so personal workspace state
  doesn't leak into the public repo.
- The repo itself remains a Python package first, vault second.

Frontmatter and conventions follow the parent vault's CLAUDE.md:
`status: unread | skimmed | extracted | synthesized | needs-pdf`,
`source_type: paper | article | thread | clip`,
`project: [anki-proteus/citations]`.

**Alternatives considered:**

- Everything as Python docstrings. Rejected — PDFs and web material don't
  belong in source.
- Separate research repo. Rejected — adds friction between code changes and
  the material that motivated them.
- Single checked-in `references.md` with bibliographic info only. Rejected —
  loses the distinction between primary sources, clippings, and synthesis, and
  doesn't survive the first serious reading session.

**Status:** Scaffolded 2026-04-23.

---

## D2: Reground `matuschak_contextualized` prompt in Matuschak's canonical writing — 2026-04-23

**Problem:** The `matuschak_contextualized` variant style in `generator.py` was
named after Andy Matuschak, but the prompt text was drafted via LLM synthesis,
not from his actual writing. With the repo being shared with him, the name-drop
risks reading as performative. His 2020 essay "How to write good prompts"
articulates five principles (focused, precise, consistent, tractable,
effortful) and distinguishes four prompt categories (standard retrieval,
context-laden/scenario, salience, creative) — none of which the current prompt
explicitly invokes.

**Decision:** (Pending execution.) Rework the prompt to:

- Explicitly invoke the five principles, scoped to the scenario category.
- Name the category correctly: our variant style falls within what Matuschak
  calls **context-laden / scenario prompts**, which "help the leap from theory to
  practice." This resolves the apparent tension between consistency and
  novelty — the scenario *surface* varies (novelty) but the target concept
  and the retrieval demand stay stable (consistency). Both are present by
  design in his framework, not in conflict.
- Keep the existing "one concept only, difficulty is in the transfer" framing,
  which aligns with *focused* and *tractable*.
- Add an explicit *effortful* constraint: the learner should not be able to
  trivially infer the concept from the scenario wording.

Citation in README will link to the essay at <https://andymatuschak.org/prompts/>
with full attribution. Tests in `tests/test_core.py::TestMatuschakContextualizedStyle`
will be extended to assert the principle names appear in the system prompt.

**Alternatives considered:**

- Rename the style to `contextualized` or `scenario`. Rejected — Matuschak's
  own framework names this category; grounding the prompt is a stronger signal
  than removing the name.
- Leave the prompt as-is and cite the essay anyway. Rejected — citing a source
  that wasn't actually consulted is worse than the current silence.
- Write a second variant style (e.g., `matuschak_salience`) to cover his other
  prompt categories. Deferred — one style, done well, before expanding.

**Status:** Source pulled into
`anki-proteus-knowledge/Raw/matuschak-2020-how-to-write-good-prompts.md`.
Prompt draft will live in
`anki-proteus-knowledge/Wiki/prompt-drafts/matuschak_contextualized-v2.md`.
Execution pending.
