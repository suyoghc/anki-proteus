# Grading And Feedback

Freeform mode lets you answer the variant question before flipping the card.
Proteus can then ask the LLM to compare your response against one or two
targets:

- the AI expected answer for the variant question
- the canonical answer on the original Anki card

The goal is not to replace your self-grade. It is an extra signal: where did
your answer cover the concept, where did it drift, and what should you look at
before choosing Again, Hard, Good, or Easy?

## Feedback Modes

`feedback_mode` controls which evaluation perspectives the grader returns.

| Mode          | What it evaluates                                                    |
| ------------- | -------------------------------------------------------------------- |
| `"ai"`        | Question page only; compares your response to the AI expected answer. |
| `"canonical"` | Answer page only; compares your response to the canonical card answer. |
| `"both"`      | Both perspectives; one API call returns both.                         |

The default is `"both"`, because the two perspectives catch different things.
The AI expected answer is aligned to the generated variant; the canonical answer
keeps the original card's target in view.

## Output Schema

### Canonical Perspective

Used on the answer page.

- `alignment`: `aligned | partial | misaligned`
- `canonical_points`
- `covered_points`
- `missed_points`
- `coverage_pct`

### AI Answer Perspective

Used on the question page when `feedback_mode` is `"ai"` or `"both"`.

- `expected_answer`
- `ai_covered_points`
- `ai_missed_points`
- `ai_coverage_pct`

### Shared Fields

- `learning_feedback`
- `incorrect`
- `overall`

## Grading Semantics

- The coverage donut uses grayscale: dark means covered, light means uncovered.
- A misaligned variant returns `Question drifted from canonical target.`
- The AI coverage donut on the question side is controlled by
  `show_ai_coverage`, which defaults to `false`.
- `grading_model` can override the main generation model for grading only.
- `grading_max_tokens` limits grading output length.
- `grading_timeout_s` keeps review from waiting too long on feedback.

## Dictation Timing

`submit_delay_ms` adds a short delay before submitting a freeform answer. This
is mainly for dictation tools that may still be finalizing text when you hit the
submit shortcut.

## How To Read The Feedback

Treat the feedback as a second opinion, not a verdict. If it says you missed a
point, check whether that point is actually central to the card. If it says your
answer is good, still ask whether *you* felt retrieval happen or whether you
recognized the shape of the prompt.

Proteus deliberately leaves the Anki scheduler alone. You still choose the
grade, and only the original card is scheduled.
