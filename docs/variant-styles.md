# Variant Styles

Proteus's variant styles are the main design surface of the add-on. They decide
what *kind* of retrieval prompt you see: a cleaner wording, a short scenario, a
contrast with a nearby concept, a transfer task, or a visual labeling prompt.

Each eligible review samples one style from `variant_style`. The goal is not
just to change the words on the card, but to keep you from settling into a
single fixed parsing stance.

## Sampling

Styles are configured through **Tools -> Proteus -> Variant Styles...** or in
`config.json` under `variant_style`.

```json
"variant_style": [
    "wozniak",
    "matuschak_contextualized",
    "elaborative",
    "discrimination",
    "feynman"
]
```

A good starting set is:

- `wozniak`
- `matuschak_contextualized`
- `elaborative`
- `discrimination`
- `feynman`

Then add `transfer_code`, `transfer_stats`, `transfer_math`, or
`diagram_labeling` when those actually fit the material in your deck.

## Style Reference

### Wozniak

One concept, unambiguous wording, retrieval-focused. This is the conservative
style: it tries to preserve the original card's job while stripping away
accidental wording.

Good for: almost any deck, especially cards that are already close to atomic.

Grounding: Wozniak's twenty rules of formulating knowledge.

### Matuschak Contextualized

Embeds the concept in a short scenario the learner reasons through. This style
is for understanding, not just recognition.

Good for: conceptual cards, applied domains, and material where context helps
you test whether the concept is actually usable.

Grounding: Andy Matuschak's work on prompts for creating understanding.

### Bloom's Taxonomy

Changes the cognitive level based on card maturity. Younger cards stay closer
to remember/understand; older cards can move toward apply/analyze/evaluate.

Good for: decks where mature cards should become slightly more demanding over
time.

Caveat: the interval-to-taxonomy mapping is a Proteus heuristic. It is not
something Anderson & Krathwohl originally proposed.

### Elaborative

Turns the prompt into a "why" or "how" question. The point is causal reasoning:
you should have to explain relationships, not just name a term.

Good for: mechanisms, processes, causes, consequences, and principle-level
cards.

### Feynman

Asks for a plain-language explanation. It prefers clarity over technical
precision, which makes it useful when you want to detect whether you can
explain the idea without hiding behind jargon.

Good for: abstract ideas, definitions that are easy to parrot, and concepts
that should be explainable to a non-specialist.

### Cloze Generation

Creates a fill-in-the-blank prompt whose answer is the key term or concept.

Good for: vocabulary, named effects, formulas, functions, and crisp concepts.

### Discrimination

Asks how one concept differs from another genuinely confusable concept.

Good for: pairs or clusters that are easy to mix up, such as related statistical
tests, psychological effects, algorithms, or medical differentials.

### Real-World Examples

Asks you to identify the concept from a real, named case.

Good for: transfer and recognition in the wild.

Honesty note: this style was designed on intuition, not from a specific source.
Related literature on concrete examples exists, but it was not a direct input
for the first implementation.

### Transfer: Code

Uses a code snippet and asks you to debug, predict, or identify the technique.

Good for: programming concepts and implementation-level knowledge.

### Transfer: Stats

Uses model output, assumptions, diagnostics, or study structure.

Good for: statistical modeling, experiment design, inference, and data-analysis
cards.

### Transfer: Math

Uses an equation, derivation, or deliberately flawed step.

Good for: formulas, proof techniques, algebraic manipulation, and error
detection.

### Diagram Labeling

Creates an SVG diagram with labeled blanks such as A, B, and C.

Good for: systems, anatomy, workflows, spatial layouts, and visual models.

## Length Limits

The length limits are there to protect review flow. A variant should disturb
the wording, not become a miniature essay.

| Style                                               | Max words | Max chars |
| --------------------------------------------------- | --------- | --------- |
| Wozniak                                             | 26        | 180       |
| Bloom, Elaborative, Discrimination, Cloze, Transfer | 30        | 210       |
| Real-World                                          | 35        | 250       |
| Feynman, Matuschak Contextualized                   | 50        | 350       |

All styles also ask for short, direct sentences. The prompt constraints avoid
preamble, filler, and unnecessarily tangled subordinate clauses.

## Bloom Maturity Mapping

| Card interval | Cognitive level       |
| ------------- | --------------------- |
| <= 7 days     | Remember / Understand |
| <= 30 days    | Understand / Apply    |
| <= 90 days    | Apply / Analyze       |
| > 90 days     | Analyze / Evaluate    |

Again, this is a pragmatic Proteus mapping. The taxonomy itself is grounded in
the cited literature; this scheduler-linked use is an implementation choice.

## Fallback Behavior

Some styles are not appropriate for every card. Visual and transfer styles can
opt out when the concept is not suited to diagrams, code, stats, or equations.
When that happens, Proteus falls back to a text variant rather than forcing a
bad visual or transfer prompt.

## Upgrading From Earlier Versions

The old fused `wozniak_matuschak` style was split into two separate styles:
`wozniak` and `matuschak_contextualized`. Existing configs are migrated
automatically. If your `variant_style` contained `wozniak_matuschak`, both new
styles are added in its place to preserve the prior sampling behavior.

## Sources And Honesty

Citations in the README mark intellectual grounding, not exact provenance for
every prompt instruction. Some styles are close to a named practice; others are
heuristic implementations inspired by a broader learning-science idea.

The `real_world` style is explicitly author's own framing. It is included
because it seems useful, not because it is pretending to descend from one clean
paper trail.

See the README references for the cited sources.
