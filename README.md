# Anki Proteus

An Anki add-on that rewrites each review question on the fly using an LLM, so
you retrieve the concept rather than recognise the card.

## Why

Andy Matuschak lists *pattern matching* among the first failure modes of spaced
repetition prompts: "unusually worded questions risk memorising shape rather
than content" [[Matuschak 2020](#references)]. The failure is hard to notice in
practice. The card is answered correctly, the scheduler extends the interval,
and nothing flags that the learner recognised the wording instead of retrieving
the knowledge.

Anki Proteus inserts a rephrasing step between the scheduler and the display.
At review time, the original question is transformed into a variant that tests
the same underlying concept in different wording, a different scenario, or a
different cognitive register. You still grade against the canonical answer on
the card; only the question surface changes.

The FSRS/SM-2 scheduler is untouched, the variant is ephemeral, and only the
original card is scored.

## How it works

When a card comes up for review, the add-on:

1. Sends the original question + answer to the Anthropic API.
2. Receives a variant question that targets the same concept.
3. Displays the variant in place of the original question.
4. On flip, shows the original answer. You grade normally.

Cards are pre-generated in the background, so the review UI is never blocked
waiting on the API. Variants are cached per card, so repeat reviews draw from
a pool rather than re-generating on every show.

## Two review modes

**Flip mode (default)** — the standard Anki flow with a transformed question.
Fast, with no extra friction.

**Freeform mode** — after the variant question, a text area appears. Dictate or
type your answer (for example, via [Wispr Flow](https://www.wispr.flow/)),
submit, and the grading feedback fills in. The expected answer is
pre-fetched alongside the variant, so it appears instantly; the grading call
runs in parallel. Flip to see the canonical answer and the feedback panel.

Toggle modes with **Ctrl+Shift+V** (Cmd+Shift+V on macOS) or in the config.

## Variant styles

Each eligible review picks a style at random from the configured set. Styles
are selected via **Tools → Proteus → Variant Styles…** and listed in
`config.json` under `variant_style`.

Each style's citation below marks the intellectual lineage it draws from,
not the exact source of the prompt text. See
[Sources & honesty](#sources--honesty) for which sources were read
first-hand versus invoked as canonical references.

### Core

| Style                        | What it does                                                              | Grounding                 |
|------------------------------|---------------------------------------------------------------------------|---------------------------|
| **Wozniak**                  | One concept, unambiguous wording, retrieval-focused.                      | [1]                       |
| **Matuschak Contextualized** | Embeds the concept in a 2–3 sentence scenario the learner reasons through. | [2]                       |
| **Bloom's Taxonomy**         | Cognitive level scales with card maturity (interval).                     | [3] (with caveat, below)  |
| **Elaborative**              | "Why" or "how" questions forcing causal reasoning.                        | [4]                       |
| **Feynman**                  | "Explain simply" framing. Clarity over precision.                         | [5], [6]                  |
| **Cloze Generation**         | Fill-in-the-blank producing the key term.                                 | [7], [8]                  |

> **Bloom caveat.** The mapping from card interval to cognitive level is a
> heuristic, not something Anderson & Krathwohl propose. The canonical source
> grounds the taxonomy itself, not its use as a scheduler-linked difficulty dial.

> **Upgrading from earlier versions.** The old fused `wozniak_matuschak` style
> was split into two separate styles (`wozniak` and `matuschak_contextualized`)
> in April 2026. Existing configs are migrated automatically: if your
> `variant_style` contained `wozniak_matuschak`, both new styles are added in
> its place to preserve the prior sampling behaviour.

### Contrast & context

| Style                  | What it does                                                 | Grounding                          |
|------------------------|--------------------------------------------------------------|------------------------------------|
| **Discrimination**     | "How does X differ from Y?" with genuinely confusable concepts. | [9], [10]                       |
| **Real-World Examples** | Identify the concept from a real, named case.               | Author's own framing (see below).  |

> **Real-World honesty.** The `real_world` style was designed on intuition, not
> from a specific source. Related literature on concrete examples exists
> (for example, Rawson, Thomas & Jacoby 2015), but was not a direct input.

### Transfer

| Style              | What it does                                                | Grounding |
|--------------------|-------------------------------------------------------------|-----------|
| **Transfer: Code**  | Debug, predict, or identify the technique in a code snippet. | [11]      |
| **Transfer: Stats** | Interpret model output or diagnose assumptions.              | [11]      |
| **Transfer: Math**  | Find the error or identify the technique in an equation.     | [11]      |

### Visual

| Style                | What it does                                           | Grounding |
|----------------------|--------------------------------------------------------|-----------|
| **Diagram Labeling** | SVG diagram with labeled blanks (A, B, C) to identify. | [12]      |

### Per-style length limits

| Style                                           | Max words | Max chars |
|-------------------------------------------------|-----------|-----------|
| Wozniak                                         | 26        | 180       |
| Bloom, Elaborative, Discrimination, Cloze, Transfer | 30    | 210       |
| Real-World                                      | 35        | 250       |
| Feynman, Matuschak Contextualized               | 50        | 350       |

### Bloom's maturity mapping

| Card interval | Cognitive level          |
|---------------|--------------------------|
| ≤ 7 days      | Remember / Understand    |
| ≤ 30 days     | Understand / Apply       |
| ≤ 90 days     | Apply / Analyze          |
| > 90 days     | Analyze / Evaluate       |

## Features

- **Batch prefetching.** Variants and expected answers for upcoming cards are
  generated in parallel background threads at session start.
- **Variant caching.** A SQLite cache stores multiple variants per card, so
  repeat reviews draw from a pool rather than re-calling the API.
- **Configurable styles.** Twelve styles available; pick any subset.
- **Personal learner context.** Describe yourself and your current focus to
  shape how variants are framed.
- **Dual-perspective grading.** Freeform responses are evaluated against the
  AI expected answer (question page) and the canonical answer (answer page).
  Controlled by `feedback_mode`.
- **Pre-fetched expected answers.** The expected answer appears instantly on
  submit; grading fills in asynchronously.
- **Card ideas.** Save interesting variants via the bookmark button; review and
  convert to real cards from the Card Ideas dialog.
- **Quick save.** 💾 saves the variant Q+A as a new card. 📋 opens Add Note
  pre-filled. ➕ opens a blank Add Note.
- **Human-in-the-loop editing.** In the Card Ideas dialog, edit draft wording,
  apply reason tags, regenerate with targeted instructions.
- **Master toggle.** `Ctrl+Shift+P` turns Proteus on/off for the session.
- **Back to question.** `Ctrl+Shift+Left` returns to the question side from the
  answer side with state preserved.
- **Refresh variant cache.** Clear and regenerate all cached variants from the
  Proteus menu.
- **Usage budget.** Set a dollar cap to track API spend.

## Behaviour

### Which cards become variants

A card is eligible only if all of the following pass:

- `enabled` is `true`
- `api_key` is set
- the note type is not in `exclude_note_types`
- the extracted question text is at least 10 characters
- the deck matches `active_decks` (when configured)
- the interval meets `min_interval_days`
- the random roll passes `transform_percent`

Review-time replacement uses cached variants only. The UI is never blocked on
a synchronous API call.

### Feedback modes

`feedback_mode` controls which evaluation perspectives the grader returns:

- `"ai"`: question page only; response compared against the AI expected answer.
- `"canonical"`: answer page only; response compared against the canonical
  flashcard answer.
- `"both"`: both perspectives; one API call returns both.

### Grading semantics

- The coverage donut uses grayscale (dark = covered, light = uncovered).
- A misaligned variant returns: `Question drifted from canonical target.`
- The AI coverage donut on the question side is controlled by
  `show_ai_coverage` (default off).

### Variant generation constraints

- All styles enforce short, direct sentences (max 12 words per sentence).
- No preamble, no subordinate clauses, no filler words.
- Visual styles (diagram, transfer) can opt out when the concept isn't suited,
  falling back to text.

### Card-creation buttons

- 🔖 save as a card idea (review later in the Card Ideas dialog).
- ➕ blank Add Note.
- 💾 quick save of variant Q + AI expected answer as a new card.
- 📋 pre-filled Add Note for editing before save.

## Compatibility

Tested on Anki 25.02+ (Python 3.9, Qt 6). Uses `gui_hooks.card_will_show` with
context strings `reviewQuestion` and `reviewAnswer`.

## Installation

1. Find your Anki add-ons folder:
   - **Tools → Add-ons → Open Add-ons Folder** in Anki.
   - Or: `~/.local/share/Anki2/addons21/` (Linux),
     `~/Library/Application Support/Anki2/addons21/` (macOS),
     `%APPDATA%\Anki2\addons21\` (Windows).

2. Copy the `anki_proteus` folder into the add-ons folder (or symlink it for
   development).

3. Restart Anki.

4. Configure your API key:
   - **Tools → Add-ons** → select *Proteus* → **Config**
   - Set `"api_key"` to your Anthropic API key.

   > **Security note on API-key storage.** Anki stores add-on config values
   > (including `api_key`) in plaintext inside its profile folder — typically
   > `~/Library/Application Support/Anki2/<profile>/addons21/<addon-id>/meta.json`
   > on macOS, or the equivalent path on Windows/Linux. That file is readable
   > by any process running as your user. Don't sync the profile folder to a
   > shared drive, don't commit a real key into `config.json`, and rotate the
   > key if you suspect the file was exposed.

## Configuration

Edit via **Tools → Add-ons → Config** or directly in `config.json`:

```json
{
    "enabled": true,
    "api_key": "",
    "model": "claude-sonnet-4-20250514",
    "response_mode": "flip",
    "active_decks": [],
    "transform_percent": 80,
    "min_interval_days": 0,
    "max_cached_variants": 3,
    "system_prompt": "",
    "learner_context": "",
    "exclude_note_types": ["Image Occlusion"],
    "variant_style": ["wozniak"],
    "batch_prefetch_count": 15,
    "batch_prefetch_concurrency": 3,
    "show_prefetch_progress": true,
    "debug_logging": false,
    "usage_budget": 5.00,
    "grading_model": "",
    "grading_max_tokens": 280,
    "grading_timeout_s": 10,
    "feedback_mode": "both",
    "show_ai_coverage": false
}
```

### Key settings

**`variant_style`**: list of styles to sample from. Each card picks one at
random. Valid values: `wozniak`, `matuschak_contextualized`, `bloom`,
`elaborative`, `feynman`, `cloze_generation`, `discrimination`, `real_world`,
`transfer_code`, `transfer_stats`, `transfer_math`, `diagram_labeling`.

**`learner_context`**: personal context injected into every variant prompt.
Describe yourself and what's currently relevant. Set via the Variant Styles
dialog.

**`feedback_mode`**: which grading perspectives to return. `"both"` (default),
`"ai"`, or `"canonical"`.

**`show_ai_coverage`**: show the AI coverage donut on the question side.
Default `false`.

**`active_decks`**: filter which decks get variants. Supports partial matching.
Empty means all decks.

**`transform_percent`**: below 100, some reviews show the original question.
80 means roughly 1 in 5 reviews is unmodified.

**`min_interval_days`**: only transform cards above this interval. Set to 7
or 14 to restrict to mature cards.

**`system_prompt`**: domain-specific context for variant quality.

**`grading_model`**: optional faster or cheaper model for grading only.

**`grading_max_tokens`**: max output tokens for grading. Default 280.

**`debug_logging`**: write diagnostic logs to `proteus_diag.log`.

## Coverage & gaps output schema

**Canonical perspective** (answer page):
- `alignment`: `aligned | partial | misaligned`
- `canonical_points`, `covered_points`, `missed_points`, `coverage_pct`

**AI answer perspective** (question page, `"ai"` or `"both"` mode):
- `expected_answer`, `ai_covered_points`, `ai_missed_points`, `ai_coverage_pct`

**Shared**: `learning_feedback`, `incorrect`, `overall`.

## Cost

- **Flip mode.** Roughly one API call per transformed review; caching reduces
  this.
- **Freeform mode.** Roughly one additional API call per review for grading.
- Text variant: ~\$0.003 each. Visual/transfer: ~\$0.006–0.010 each (more
  output tokens).
- Freeform grading: ~\$0.006 per call (`"ai"` or `"canonical"`), ~\$0.009 per
  call (`"both"`).
- At 50 reviews per day: roughly \$0.12–0.25/day in flip mode, \$0.30–0.50/day
  in freeform mode.

## Keyboard shortcuts

- **Ctrl+Shift+P**: toggle Proteus on/off (Cmd+Shift+P on macOS).
- **Ctrl+Shift+V**: toggle flip/freeform mode (Cmd+Shift+V on macOS).
- **Ctrl+Shift+Left**: back to question from the answer side
  (Cmd+Shift+Left on macOS).
- **Enter** or the submit button (freeform): submit the response and start
  grading.

## Tools menu

```
Tools → Proteus →
    Card Ideas
    Usage Stats
    Variant Styles...
    Refresh Variant Cache
    ─────────────
    Toggle On/Off         Ctrl+Shift+P
    Toggle Flip/Freeform  Ctrl+Shift+V
    Back to Question      Ctrl+Shift+Left
```

## Files

```
anki_proteus/
├── __init__.py         Hooks, UI injection, review flow.
├── generator.py        LLM API calls, variant styles, grading.
├── cache.py            SQLite cache (schema v9) for variants + card ideas.
├── prefetch.py         Single-card background prefetch thread.
├── batch_prefetch.py   Parallel batch pre-generation at session start.
├── config.json         Default configuration.
├── manifest.json       Anki add-on metadata.
├── tests/test_core.py  Unit tests.
└── README.md
```

## Development

Symlink the repo into your add-ons folder:

```bash
ln -s /path/to/anki-proteus ~/Library/Application\ Support/Anki2/addons21/anki_proteus
```

Changes take effect on Anki restart. Run tests:

```bash
pytest -v
```

Design decisions are logged in `Notes/DECISIONS.md`.

## Future directions

- **Web search for discrimination**: fetch commonly confused concepts to
  build better contrasts.
- **Chrome extension**: generate cards from web page content using the same
  prompt engineering.
- **Visual styles**: error detection, flowchart completion, graph
  interpretation.
- **Keyword similarity**: client-side sanity check on LLM coverage verdicts.
- **Pretesting**: show variants before first review (productive failure).

## Sources & honesty

Citations below are the intellectual grounding for each variant style.
Honesty note: of the twelve sources, only Matuschak (2020) was read in full
before the prompts were written. The others are canonical references for
well-known effects (generation, desirable difficulties, transfer-appropriate
processing, dual coding) that inform the design in spirit. A private reading
trail is maintained outside this repository and the prompts will be revised
as each source is extracted; see `Notes/DECISIONS.md` (D2) for the rework
of `matuschak_contextualized` against Matuschak's five principles.

The `real_world` style is flagged as author's own framing, not derived from
a source.

## References

[1] Wozniak, P. (2000). *Effective learning: Twenty rules of formulating
knowledge.* SuperMemo. <https://super-memory.com/articles/20rules.htm>

[2] Matuschak, A. (2020). *How to write good prompts: using spaced repetition
to create understanding.* <https://andymatuschak.org/prompts/>

[3] Anderson, L. W., & Krathwohl, D. R. (Eds.). (2001). *A Taxonomy for
Learning, Teaching, and Assessing: A Revision of Bloom's Taxonomy of
Educational Objectives.* Longman.

[4] Dunlosky, J., Rawson, K. A., Marsh, E. J., Nathan, M. J., & Willingham,
D. T. (2013). Improving students' learning with effective learning techniques:
Promising directions from cognitive and educational psychology.
*Psychological Science in the Public Interest*, 14(1), 4–58.
<https://doi.org/10.1177/1529100612453266>

[5] Chi, M. T. H., Bassok, M., Lewis, M. W., Reimann, P., & Glaser, R. (1989).
Self-explanations: How students study and use examples in learning to solve
problems. *Cognitive Science*, 13(2), 145–182.
<https://doi.org/10.1207/s15516709cog1302_1>

[6] Fiorella, L., & Mayer, R. E. (2013). The relative benefits of learning by
teaching and learning by preparing to teach. *Contemporary Educational
Psychology*, 38(4), 281–288.
<https://doi.org/10.1016/j.cedpsych.2013.06.001>

[7] Slamecka, N. J., & Graf, P. (1978). The generation effect: Delineation of
a phenomenon. *Journal of Experimental Psychology: Human Learning and Memory*,
4(6), 592–604. <https://doi.org/10.1037/0278-7393.4.6.592>

[8] Bjork, R. A. (1994). Memory and metamemory considerations in the training
of human beings. In J. Metcalfe & A. P. Shimamura (Eds.), *Metacognition:
Knowing about knowing* (pp. 185–205). MIT Press.

[9] Rohrer, D., Dedrick, R. F., & Burgess, K. (2014). The benefit of
interleaved mathematics practice is not limited to superficially similar kinds
of problems. *Psychonomic Bulletin & Review*, 21(5), 1323–1330.
<https://doi.org/10.3758/s13423-014-0588-3>

[10] Kornell, N., & Bjork, R. A. (2008). Learning concepts and categories:
Is spacing the "enemy of induction"? *Psychological Science*, 19(6), 585–592.
<https://doi.org/10.1111/j.1467-9280.2008.02127.x>

[11] Morris, C. D., Bransford, J. D., & Franks, J. J. (1977). Levels of
processing versus transfer appropriate processing. *Journal of Verbal
Learning and Verbal Behavior*, 16(5), 519–533.
<https://doi.org/10.1016/S0022-5371(77)80016-9>

[12] Clark, J. M., & Paivio, A. (1991). Dual coding theory and education.
*Educational Psychology Review*, 3(3), 149–210.
<https://doi.org/10.1007/BF01320076>
