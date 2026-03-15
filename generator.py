"""
LLM-powered variant generation and response grading.

Uses the Anthropic API to:
1. Generate novel question variants that test the same concept
2. Grade freeform (spoken/typed) responses against canonical answers
"""

import json
import os
import re
import threading
import urllib.request
import urllib.error
from typing import Optional

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-20250514"

# ---------------------------------------------------------------------------
# Token Usage Tracking
# ---------------------------------------------------------------------------

ADDON_DIR = os.path.dirname(__file__)
_LOG_PATH = os.path.join(ADDON_DIR, "proteus_diag.log")
_USAGE_PATH = os.path.join(ADDON_DIR, "usage.json")


def diag_log(msg: str, debug: bool = True):
    """Shared diagnostic logger. Writes to proteus_diag.log when debug is True."""
    if not debug:
        return
    import time as _t
    ts = _t.strftime("%H:%M:%S")
    try:
        with open(_LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass
_usage_lock = threading.Lock()
_usage_tracker = None  # lazy-loaded


def _load_usage() -> dict:
    """Load usage stats from disk, or return fresh counters."""
    try:
        with open(_USAGE_PATH, "r") as f:
            data = json.load(f)
            return {
                "input_tokens": data.get("input_tokens", 0),
                "output_tokens": data.get("output_tokens", 0),
                "api_calls": data.get("api_calls", 0),
            }
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}


def _save_usage(data: dict):
    """Persist usage stats to disk."""
    try:
        with open(_USAGE_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _get_tracker() -> dict:
    """Lazy-init and return the usage tracker."""
    global _usage_tracker
    if _usage_tracker is None:
        _usage_tracker = _load_usage()
    return _usage_tracker


def _record_usage(input_tokens: int, output_tokens: int):
    """Thread-safe: accumulate token counts and persist."""
    with _usage_lock:
        tracker = _get_tracker()
        tracker["input_tokens"] += input_tokens
        tracker["output_tokens"] += output_tokens
        tracker["api_calls"] += 1
        _save_usage(tracker)


def get_usage() -> dict:
    """Return a copy of the current usage stats."""
    with _usage_lock:
        return dict(_get_tracker())


def reset_usage():
    """Zero all counters and persist."""
    global _usage_tracker
    with _usage_lock:
        _usage_tracker = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}
        _save_usage(_usage_tracker)

# ---------------------------------------------------------------------------
# Variant Generation
# ---------------------------------------------------------------------------

VARIANT_SYSTEM_PROMPT = """You are a question variant generator for a spaced repetition system.

Your job: given an original flashcard (question + answer), generate a SINGLE new question
that tests the SAME underlying concept but looks different.

Rules:
- The new question must be answerable using the same knowledge as the original answer.
- Vary the format: sometimes rephrase, sometimes pose a scenario, sometimes ask
  "what would go wrong if...", sometimes ask the learner to explain why, sometimes
  present an error to identify.
- Do NOT make the question significantly harder or easier than the original.
- Do NOT include the answer in your question.
- Return ONLY the new question text. No preamble, no explanation, no labels.
- Keep it concise — never longer than the original question.
- Communicate minimalistically, prioritizing clarity and engagement.
- Use plain text (no markdown formatting).
"""

VARIANT_USER_TEMPLATE = """Original question: {question}

Original answer: {answer}

{domain_context}

Generate one variant question that tests the same concept."""


_MAX_VARIANT_WORDS = 26
_MAX_VARIANT_CHARS = 180

_VARIANT_SHORTEN_SYSTEM_PROMPT = """You shorten flashcard questions while preserving the tested concept.

Rules:
- Keep the same answer target as the original.
- Keep one clear ask only.
- Keep wording plain and concrete.
- Output must be <= 26 words and <= 180 characters.
- Return ONLY the rewritten question text.
"""

_VARIANT_SHORTEN_TEMPLATE = """Original question: {question}

Original answer: {answer}

Current variant: {variant}

Rewrite it to be concise while preserving the same answer target."""


def _normalize_variant_text(text: str) -> str:
    """Normalize whitespace in generated variant text."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _variant_too_long(text: str) -> bool:
    """True when variant exceeds configured word/char limits."""
    if not text:
        return False
    return len(text.split()) > _MAX_VARIANT_WORDS or len(text) > _MAX_VARIANT_CHARS


def _hard_limit_variant(text: str) -> str:
    """Force a variant into length limits as a final fallback."""
    variant = _normalize_variant_text(text)
    if not variant:
        return ""

    q_idx = variant.find("?")
    if q_idx != -1 and (q_idx + 1) <= _MAX_VARIANT_CHARS:
        variant = variant[: q_idx + 1].strip()

    words = variant.split()
    if len(words) > _MAX_VARIANT_WORDS:
        variant = " ".join(words[:_MAX_VARIANT_WORDS]).rstrip(" ,;:.")

    if len(variant) > _MAX_VARIANT_CHARS:
        clipped = variant[:_MAX_VARIANT_CHARS]
        if " " in clipped:
            clipped = clipped.rsplit(" ", 1)[0]
        variant = clipped.rstrip(" ,;:.")

    if variant and not variant.endswith("?"):
        if len(variant) >= _MAX_VARIANT_CHARS:
            variant = variant[: _MAX_VARIANT_CHARS - 1].rstrip(" ,;:.")
        variant = variant + "?"

    return variant


def generate_variant(question: str, answer: str, config: dict) -> Optional[str]:
    """
    Generate a variant question via LLM.

    Returns the variant text, or None on failure.
    """
    api_key = config.get("api_key", "")
    if not api_key:
        return None

    model = config.get("model", DEFAULT_MODEL)

    domain_ctx = ""
    if config.get("system_prompt"):
        domain_ctx = f"Domain context: {config['system_prompt']}"

    user_msg = VARIANT_USER_TEMPLATE.format(
        question=question,
        answer=answer,
        domain_context=domain_ctx,
    )

    system = VARIANT_SYSTEM_PROMPT.strip()

    raw = _call_api(api_key, model, system, user_msg, max_tokens=300)
    if not raw:
        return None

    variant = _normalize_variant_text(raw)
    if not variant:
        return None

    # One shorten pass for overlong generations before hard-capping.
    if _variant_too_long(variant):
        shorten_user_msg = _VARIANT_SHORTEN_TEMPLATE.format(
            question=question,
            answer=answer,
            variant=variant,
        )
        shortened = _call_api(
            api_key,
            model,
            _VARIANT_SHORTEN_SYSTEM_PROMPT.strip(),
            shorten_user_msg,
            max_tokens=120,
        )
        if shortened:
            variant = _normalize_variant_text(shortened)

    if _variant_too_long(variant):
        variant = _hard_limit_variant(variant)

    return variant or None


# ---------------------------------------------------------------------------
# Response Grading
# ---------------------------------------------------------------------------

GRADING_SYSTEM_PROMPT = """You are a response evaluator for a spaced repetition system.

The learner was shown a question and gave a spoken/typed response. Your job is to
evaluate whether their response demonstrates understanding of the concept.

First, decide whether the shown question is aligned to the canonical answer target.

Rules:
- The response may be voice-transcribed: ignore filler words, disfluencies, grammar
  issues, and informal phrasing. Evaluate ONLY conceptual correctness.
- Compare against the canonical answer provided.
- Be encouraging but honest.
- Do NOT repeat the full answer back to them — they'll see the original answer
  alongside your evaluation.
- If question and canonical answer are misaligned, DO NOT grade correctness.
- Always provide useful related-learning observations in "learning_feedback".

Return your evaluation as a JSON object with exactly these keys:
- "alignment": string — one of "aligned", "partial", "misaligned"
- "alignment_note": string — short reason for the alignment judgment
- "expected_answer": string — concise answer target for the shown question
- "canonical_points": array of strings — core answer points to check
- "covered_points": array of strings — canonical points the learner covered
- "missed_points": array of strings — canonical points the learner missed
- "coverage_pct": integer 0..100, based only on canonical coverage
- "question_gap_points": array of strings — canonical points not really tested by the shown question/new expected target
- "learning_feedback": array of strings — concise related insights (can be empty)
- "incorrect": array of strings — things the learner stated incorrectly (empty array if none)
- "overall": string — 1 sentence summary of their performance

Coverage rule:
- If alignment is "misaligned", set "coverage_pct" to 0 and set
  "canonical_points"/"covered_points"/"missed_points"/"incorrect" to empty arrays.
- If alignment is "misaligned", set "question_gap_points" to the key missing canonical points.
- Otherwise ensure covered_points + missed_points map to canonical_points.

Output limits (strict):
- "alignment_note": max 18 words.
- "expected_answer": max 28 words.
- "overall": max 18 words.
- Each array item: max 14 words.
- Max 2 items in "learning_feedback".
- Max 3 items each in "canonical_points", "covered_points", and "missed_points".
- Max 3 items in "question_gap_points".
- Max 2 items in "incorrect".

Keep each bullet point to one concise sentence. Return ONLY the JSON object, no markdown fences."""

GRADING_USER_TEMPLATE = """Question shown: {question}

Canonical answer: {answer}

Learner's response: {response}

Evaluate their response as JSON."""


GRADING_SYSTEM_PROMPT_AI = """You are a response evaluator for a spaced repetition system.

The learner was shown a question and gave a spoken/typed response. Your job is to
determine the ideal answer for the shown question and evaluate the response against it.

Rules:
- The response may be voice-transcribed: ignore filler words, disfluencies, grammar
  issues, and informal phrasing. Evaluate ONLY conceptual correctness.
- First determine what the ideal answer to the shown question would be.
- Evaluate the response against that ideal answer — NOT against any external reference.
- Be encouraging but honest.
- Always provide useful related-learning observations in "learning_feedback".

Return your evaluation as a JSON object with exactly these keys:
- "expected_answer": string — concise ideal answer for the shown question
- "ai_covered_points": array of strings — expected-answer points the learner addressed
- "ai_missed_points": array of strings — expected-answer points the learner missed
- "ai_coverage_pct": integer 0..100, based on expected-answer coverage
- "learning_feedback": array of strings — concise related insights (can be empty)
- "incorrect": array of strings — things the learner stated incorrectly (empty array if none)
- "overall": string — 1 sentence summary of their performance

Output limits (strict):
- "expected_answer": max 28 words.
- "overall": max 18 words.
- Each array item: max 14 words.
- Max 2 items in "learning_feedback".
- Max 3 items each in "ai_covered_points" and "ai_missed_points".
- Max 2 items in "incorrect".

Keep each bullet point to one concise sentence. Return ONLY the JSON object, no markdown fences."""

GRADING_USER_TEMPLATE_AI = """Question shown: {question}

Learner's response: {response}

Evaluate their response as JSON."""


GRADING_SYSTEM_PROMPT_BOTH = """You are a response evaluator for a spaced repetition system.

The learner was shown a question and gave a spoken/typed response. Your job is to
evaluate their response from TWO perspectives: against the question's own ideal answer,
and against the canonical flashcard answer.

First, decide whether the shown question is aligned to the canonical answer target.

Rules:
- The response may be voice-transcribed: ignore filler words, disfluencies, grammar
  issues, and informal phrasing. Evaluate ONLY conceptual correctness.
- Be encouraging but honest.
- If question and canonical answer are misaligned, DO NOT grade canonical correctness.
- Always provide useful related-learning observations in "learning_feedback".

Return your evaluation as a JSON object with exactly these keys:

AI answer perspective (vs the question's ideal answer):
- "expected_answer": string — concise answer target for the shown question
- "ai_covered_points": array of strings — expected-answer points the learner addressed
- "ai_missed_points": array of strings — expected-answer points the learner missed
- "ai_coverage_pct": integer 0..100, based on expected-answer coverage

Canonical answer perspective (vs the flashcard's canonical answer):
- "alignment": string — one of "aligned", "partial", "misaligned"
- "alignment_note": string — short reason for the alignment judgment
- "canonical_points": array of strings — core canonical answer points
- "covered_points": array of strings — canonical points the learner covered
- "missed_points": array of strings — canonical points the learner missed
- "coverage_pct": integer 0..100, based on canonical coverage
- "question_gap_points": array of strings — canonical points not tested by the shown question

Shared fields:
- "learning_feedback": array of strings — concise related insights (can be empty)
- "incorrect": array of strings — things the learner stated incorrectly (empty array if none)
- "overall": string — 1 sentence summary of their performance

Coverage rules:
- If alignment is "misaligned", set canonical coverage fields to empty/0.
- AI coverage is always evaluated (even when canonical alignment is misaligned).

Output limits (strict):
- "alignment_note": max 18 words.
- "expected_answer": max 28 words.
- "overall": max 18 words.
- Each array item: max 14 words.
- Max 2 items in "learning_feedback".
- Max 3 items each in all point arrays.
- Max 2 items in "incorrect".

Keep each bullet point to one concise sentence. Return ONLY the JSON object, no markdown fences."""


def _decode_json_fragment(text: str) -> str:
    """Decode a JSON-escaped string fragment; best effort."""
    try:
        return str(json.loads('"%s"' % text))
    except Exception:
        return text


def _extract_json_string_field(raw: str, key: str) -> str:
    """Extract a JSON string field from possibly-truncated JSON text."""
    pattern = r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(key)
    match = re.search(pattern, raw, re.S)
    if not match:
        return ""
    return _decode_json_fragment(match.group(1)).strip()


def _extract_json_int_field(raw: str, key: str, default: int = 0) -> int:
    """Extract an integer field from possibly-truncated JSON text."""
    pattern = r'"%s"\s*:\s*(-?\d+)' % re.escape(key)
    match = re.search(pattern, raw)
    if not match:
        return default
    try:
        return int(match.group(1))
    except Exception:
        return default


def _extract_json_string_list_field(raw: str, key: str, limit: int = 3) -> list:
    """Extract list-of-string field from possibly-truncated JSON text."""
    pattern_closed = r'"%s"\s*:\s*\[(.*?)\]' % re.escape(key)
    match = re.search(pattern_closed, raw, re.S)
    segment = ""
    if match:
        segment = match.group(1)
    else:
        pattern_open = r'"%s"\s*:\s*\[(.*)$' % re.escape(key)
        match = re.search(pattern_open, raw, re.S)
        if match:
            segment = match.group(1)
    if not segment:
        return []

    items = []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', segment):
        text = _decode_json_fragment(m.group(1)).strip()
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _uniq_clean_list(items, limit):
    # type: (list, int) -> list
    """Normalize list-like values into deduplicated short string lists."""
    if items is None:
        items = []
    elif not isinstance(items, (list, tuple)):
        items = [items]
    out = []
    seen = set()
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _normalize_grading_payload(data: dict) -> dict:
    """Normalize and guard grading payload fields."""
    alignment = str(data.get("alignment", "aligned")).strip().lower()
    if alignment not in ("aligned", "partial", "misaligned"):
        alignment = "aligned"

    alignment_note = str(data.get("alignment_note", "")).strip()
    expected_answer = str(data.get("expected_answer", "")).strip()
    learning_feedback = _uniq_clean_list(data.get("learning_feedback", []), limit=2)
    canonical_points = _uniq_clean_list(data.get("canonical_points", []), limit=3)
    covered_points = _uniq_clean_list(
        data.get("covered_points", data.get("correct", [])),
        limit=3,
    )
    missed_points = _uniq_clean_list(
        data.get("missed_points", data.get("missed", [])),
        limit=3,
    )
    question_gap_points = _uniq_clean_list(data.get("question_gap_points", []), limit=3)
    incorrect = _uniq_clean_list(data.get("incorrect", []), limit=2)
    overall = str(data.get("overall", "")).strip()

    if not canonical_points:
        canonical_points = _uniq_clean_list(covered_points + missed_points, limit=3)

    if not expected_answer and canonical_points:
        expected_answer = "; ".join(canonical_points[:3])

    if canonical_points:
        canonical_set = set(canonical_points)
        if not covered_points and missed_points:
            missed_set = set(missed_points)
            covered_points = [p for p in canonical_points if p not in missed_set]
        if not missed_points and covered_points:
            covered_set = set(covered_points)
            missed_points = [p for p in canonical_points if p not in covered_set]
        else:
            # Trim any non-canonical drift from covered/missed lists.
            covered_points = [p for p in covered_points if p in canonical_set]
            missed_points = [p for p in missed_points if p in canonical_set]

    raw_coverage_pct = None  # type: Optional[int]
    try:
        if data.get("coverage_pct") is not None:
            raw_coverage_pct = int(data.get("coverage_pct"))
    except Exception:
        raw_coverage_pct = None

    coverage_pct = None  # type: Optional[int]

    if alignment == "misaligned":
        canonical_points = []
        covered_points = []
        missed_points = []
        incorrect = []
        coverage_pct = 0
        score = 0
        if not question_gap_points:
            question_gap_points = _uniq_clean_list(
                data.get("canonical_points", data.get("missed", [])),
                limit=3,
            )
        if not overall:
            overall = "Question drifted from canonical target."
    else:
        denom = len(canonical_points)
        if denom <= 0:
            denom = len(covered_points) + len(missed_points)
        if denom > 0:
            covered_n = min(len(covered_points), denom)
            coverage_pct = int(round((100.0 * covered_n) / float(denom)))
        elif raw_coverage_pct is not None:
            coverage_pct = raw_coverage_pct
        if coverage_pct is not None:
            coverage_pct = max(0, min(100, int(coverage_pct)))
        if not question_gap_points:
            question_gap_points = list(missed_points)

    if alignment != "misaligned":
        try:
            score = int(data.get("score", 0))
        except Exception:
            score = 0

    # Normalize AI-answer-relative fields (present in "ai" and "both" modes)
    ai_covered = _uniq_clean_list(data.get("ai_covered_points", []), limit=3)
    ai_missed = _uniq_clean_list(data.get("ai_missed_points", []), limit=3)
    ai_coverage_pct = None  # type: Optional[int]
    try:
        raw_ai_pct = data.get("ai_coverage_pct")
        if raw_ai_pct is not None:
            ai_coverage_pct = int(raw_ai_pct)
    except Exception:
        pass
    if ai_covered or ai_missed:
        ai_denom = len(ai_covered) + len(ai_missed)
        if ai_denom > 0:
            ai_coverage_pct = int(round(100.0 * len(ai_covered) / ai_denom))
        if ai_coverage_pct is not None:
            ai_coverage_pct = max(0, min(100, ai_coverage_pct))

    result = {
        "alignment": alignment,
        "alignment_note": alignment_note,
        "expected_answer": expected_answer,
        "canonical_points": canonical_points,
        "covered_points": covered_points,
        "missed_points": missed_points,
        "coverage_pct": coverage_pct,
        "question_gap_points": question_gap_points,
        "learning_feedback": learning_feedback,
        # Back-compat aliases for older UI paths/tests.
        "correct": covered_points,
        "incorrect": incorrect,
        "missed": missed_points,
        "overall": overall,
        "score": score,
    }
    if ai_covered or ai_missed or ai_coverage_pct is not None:
        result["ai_covered_points"] = ai_covered
        result["ai_missed_points"] = ai_missed
        result["ai_coverage_pct"] = ai_coverage_pct
    return result


def _parse_partial_grading_payload(raw: str) -> Optional[dict]:
    """Best-effort parse of truncated/non-JSON grading text."""
    if not raw:
        return None

    data = {
        "alignment": _extract_json_string_field(raw, "alignment") or "aligned",
        "alignment_note": _extract_json_string_field(raw, "alignment_note"),
        "expected_answer": _extract_json_string_field(raw, "expected_answer"),
        "canonical_points": _extract_json_string_list_field(raw, "canonical_points", limit=3),
        "covered_points": _extract_json_string_list_field(raw, "covered_points", limit=3),
        "missed_points": _extract_json_string_list_field(raw, "missed_points", limit=3),
        "coverage_pct": _extract_json_int_field(raw, "coverage_pct", default=None),
        "question_gap_points": _extract_json_string_list_field(raw, "question_gap_points", limit=3),
        "learning_feedback": _extract_json_string_list_field(raw, "learning_feedback", limit=2),
        "correct": _extract_json_string_list_field(raw, "correct", limit=2),
        "incorrect": _extract_json_string_list_field(raw, "incorrect", limit=2),
        "missed": _extract_json_string_list_field(raw, "missed", limit=2),
        "overall": _extract_json_string_field(raw, "overall"),
        "score": _extract_json_int_field(raw, "score", default=3),
        "ai_covered_points": _extract_json_string_list_field(raw, "ai_covered_points", limit=3),
        "ai_missed_points": _extract_json_string_list_field(raw, "ai_missed_points", limit=3),
        "ai_coverage_pct": _extract_json_int_field(raw, "ai_coverage_pct", default=None),
    }

    has_signal = any([
        data["alignment_note"],
        data["expected_answer"],
        data["canonical_points"],
        data["covered_points"],
        data["missed_points"],
        data["question_gap_points"],
        data["learning_feedback"],
        data["correct"],
        data["incorrect"],
        data["missed"],
        data["overall"],
        '"alignment"' in raw,
    ])
    if not has_signal:
        return None
    return _normalize_grading_payload(data)


def grade_response(
    variant_question: str,
    user_response: str,
    canonical_answer: str,
    config: dict,
) -> Optional[dict]:
    """
    Grade a freeform response against the canonical answer.

    Returns a dict with keys: alignment, alignment_note, canonical_points,
    expected_answer, covered_points, missed_points, coverage_pct, question_gap_points,
    learning_feedback, incorrect, overall (plus back-compat aliases).
    Falls back to a neutral structured payload if JSON parsing fails.
    Returns None on API failure.
    """
    api_key = config.get("api_key", "")
    if not api_key:
        return None

    base_model = config.get("model", DEFAULT_MODEL)
    override = str(config.get("grading_model", "")).strip()
    model = override or base_model
    max_tokens = int(config.get("grading_max_tokens", 280))
    timeout_s = float(config.get("grading_timeout_s", 10))
    feedback_mode = str(config.get("feedback_mode", "canonical")).strip().lower()

    if feedback_mode == "ai":
        system = GRADING_SYSTEM_PROMPT_AI.strip()
        user_msg = GRADING_USER_TEMPLATE_AI.format(
            question=variant_question,
            response=user_response,
        )
    elif feedback_mode == "both":
        system = GRADING_SYSTEM_PROMPT_BOTH.strip()
        user_msg = GRADING_USER_TEMPLATE.format(
            question=variant_question,
            answer=canonical_answer,
            response=user_response,
        )
        max_tokens = int(max_tokens * 1.4)  # more fields to produce
    else:
        system = GRADING_SYSTEM_PROMPT.strip()
        user_msg = GRADING_USER_TEMPLATE.format(
            question=variant_question,
            answer=canonical_answer,
            response=user_response,
        )

    raw = _call_api(
        api_key,
        model,
        system,
        user_msg,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )
    if raw is None and model != base_model:
        # If grading-model override is unavailable, retry once on the base model.
        raw = _call_api(
            api_key,
            base_model,
            system,
            user_msg,
            max_tokens=max_tokens,
            timeout_s=max(timeout_s, 10),
        )
    if raw is None:
        return None

    # Debug: log raw grading response so truncation issues are visible.
    try:
        import time as _t
        with open(_LOG_PATH, "a") as _f:
            _f.write(f"{_t.strftime('%H:%M:%S')} grading raw ({len(raw)} chars): {raw[:1200].replace(chr(10), ' ')}\n")
    except Exception:
        pass

    try:
        data = json.loads(raw)
        return _normalize_grading_payload(dict(data))
    except (json.JSONDecodeError, ValueError, TypeError):
        partial = _parse_partial_grading_payload(raw)
        if partial is not None:
            return partial
        # LLM didn't return valid JSON — return neutral fallback (no raw dump)
        return {
            "alignment": "aligned",
            "alignment_note": "",
            "expected_answer": "",
            "canonical_points": [],
            "covered_points": [],
            "missed_points": [],
            "coverage_pct": None,
            "question_gap_points": [],
            "learning_feedback": [],
            "correct": [],
            "incorrect": [],
            "missed": [],
            "overall": "Evaluation unavailable.",
            "score": 0,
        }


# ---------------------------------------------------------------------------
# API Helper
# ---------------------------------------------------------------------------

def _call_api(
    api_key: str,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int = 300,
    timeout_s: float = 15,
) -> Optional[str]:
    """Make a single Anthropic API call. Returns text or None."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            # Record token usage
            usage = result.get("usage", {})
            _record_usage(
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            )
            # Extract text from response
            for block in result.get("content", []):
                if block.get("type") == "text":
                    return block["text"].strip()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"[Proteus] API error {e.code}: {error_body}")
    except Exception as e:
        print(f"[Proteus] API call failed: {e}")

    return None
