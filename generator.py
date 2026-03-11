"""
LLM-powered variant generation and response grading.

Uses the Anthropic API to:
1. Generate novel question variants that test the same concept
2. Grade freeform (spoken/typed) responses against canonical answers
"""

import json
import os
import threading
import urllib.request
import urllib.error
from typing import Optional

API_URL = "https://api.anthropic.com/v1/messages"

# ---------------------------------------------------------------------------
# Token Usage Tracking
# ---------------------------------------------------------------------------

ADDON_DIR = os.path.dirname(__file__)
_USAGE_PATH = os.path.join(ADDON_DIR, "usage.json")
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
- Keep it concise — similar length to the original question.
- Use plain text (no markdown formatting).
"""

VARIANT_USER_TEMPLATE = """Original question: {question}

Original answer: {answer}

{domain_context}

Generate one variant question that tests the same concept."""


def generate_variant(question: str, answer: str, config: dict) -> Optional[str]:
    """
    Generate a variant question via LLM.

    Returns the variant text, or None on failure.
    """
    api_key = config.get("api_key", "")
    if not api_key:
        return None

    model = config.get("model", "claude-sonnet-4-20250514")

    domain_ctx = ""
    if config.get("system_prompt"):
        domain_ctx = f"Domain context: {config['system_prompt']}"

    user_msg = VARIANT_USER_TEMPLATE.format(
        question=question,
        answer=answer,
        domain_context=domain_ctx,
    )

    system = VARIANT_SYSTEM_PROMPT.strip()

    return _call_api(api_key, model, system, user_msg, max_tokens=300)


# ---------------------------------------------------------------------------
# Response Grading
# ---------------------------------------------------------------------------

GRADING_SYSTEM_PROMPT = """You are a response evaluator for a spaced repetition system.

The learner was shown a question and gave a spoken/typed response. Your job is to
evaluate whether their response demonstrates understanding of the concept.

Rules:
- The response may be voice-transcribed: ignore filler words, disfluencies, grammar
  issues, and informal phrasing. Evaluate ONLY conceptual correctness.
- Compare against the canonical answer provided.
- Be encouraging but honest.
- Do NOT repeat the full answer back to them — they'll see the original answer
  alongside your evaluation.

Return your evaluation as a JSON object with exactly these keys:
- "correct": array of strings — key points the learner got right (empty array if none)
- "incorrect": array of strings — things the learner stated incorrectly (empty array if none)
- "missed": array of strings — important points from the canonical answer that the learner did not mention (empty array if none)
- "overall": string — 1 sentence summary of their performance
- "score": integer 1 to 5 (1=completely wrong, 3=partial, 5=perfect)

Keep each bullet point to one concise sentence. Return ONLY the JSON object, no markdown fences."""

GRADING_USER_TEMPLATE = """Question shown: {question}

Canonical answer: {answer}

Learner's response: {response}

Evaluate their response as JSON."""


def grade_response(
    variant_question: str,
    user_response: str,
    canonical_answer: str,
    config: dict,
) -> Optional[dict]:
    """
    Grade a freeform response against the canonical answer.

    Returns a dict with keys: correct, incorrect, missed, overall, score.
    Falls back to {"overall": raw_text, ...} if JSON parsing fails.
    Returns None on API failure.
    """
    api_key = config.get("api_key", "")
    if not api_key:
        return None

    model = config.get("model", "claude-sonnet-4-20250514")

    user_msg = GRADING_USER_TEMPLATE.format(
        question=variant_question,
        answer=canonical_answer,
        response=user_response,
    )

    system = GRADING_SYSTEM_PROMPT.strip()

    raw = _call_api(api_key, model, system, user_msg, max_tokens=400)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
        # Validate expected keys exist with correct types
        return {
            "correct": list(data.get("correct", [])),
            "incorrect": list(data.get("incorrect", [])),
            "missed": list(data.get("missed", [])),
            "overall": str(data.get("overall", "")),
            "score": int(data.get("score", 3)),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        # LLM didn't return valid JSON — wrap raw text as fallback
        return {
            "correct": [],
            "incorrect": [],
            "missed": [],
            "overall": raw,
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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
