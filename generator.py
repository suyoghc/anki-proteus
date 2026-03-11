"""
LLM-powered variant generation and response grading.

Uses the Anthropic API to:
1. Generate novel question variants that test the same concept
2. Grade freeform (spoken/typed) responses against canonical answers
"""

import json
import urllib.request
import urllib.error

API_URL = "https://api.anthropic.com/v1/messages"

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


def generate_variant(question: str, answer: str, config: dict) -> str | None:
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
- Be encouraging but honest. If they got it right, say so briefly. If they missed
  something important, point out what specifically.
- Keep your evaluation to 2-3 sentences max.
- Do NOT repeat the full answer back to them — they'll see the original answer
  alongside your evaluation.
- Return ONLY your evaluation text. No preamble or labels.
"""

GRADING_USER_TEMPLATE = """Question shown: {question}

Canonical answer: {answer}

Learner's response: {response}

Evaluate their response."""


def grade_response(
    variant_question: str,
    user_response: str,
    canonical_answer: str,
    config: dict,
) -> str | None:
    """
    Grade a freeform response against the canonical answer.

    Returns evaluation text, or None on failure.
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

    return _call_api(api_key, model, system, user_msg, max_tokens=200)


# ---------------------------------------------------------------------------
# API Helper
# ---------------------------------------------------------------------------

def _call_api(
    api_key: str,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int = 300,
) -> str | None:
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
            # Extract text from response
            for block in result.get("content", []):
                if block.get("type") == "text":
                    return block["text"].strip()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"[VariantReviewer] API error {e.code}: {error_body}")
    except Exception as e:
        print(f"[VariantReviewer] API call failed: {e}")

    return None
