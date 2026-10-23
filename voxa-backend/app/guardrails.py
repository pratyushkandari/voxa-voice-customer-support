"""
Ingress guardrails: PII masking and prompt-injection neutralisation.

These run on every user utterance before it is routed, embedded, cached or persisted,
so raw card numbers / SSNs never reach Redis, PostgreSQL or the worker fleet.
"""
import re
from typing import Tuple

# Regex patterns for sensitive PII
CREDIT_CARD_REGEX = re.compile(r'\b(?:\d[ -]*?){13,16}\b')
PHONE_REGEX = re.compile(r'\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')
SSN_REGEX = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')

# Prompt injection signatures to neutralize
PROMPT_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?(previous|prior)\s+instructions?', re.IGNORECASE),
    re.compile(r'system\s+prompt\s+override', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+(DAN|an\s+unfiltered|jailbroken)', re.IGNORECASE),
    re.compile(r'disregard\s+all\s+safety\s+rules', re.IGNORECASE),
    re.compile(r'output\s+the\s+system\s+prompt', re.IGNORECASE),
]

INJECTION_PLACEHOLDER = "[BLOCKED_INSTRUCTION_OVERRIDE]"


def mask_pii(text: str) -> Tuple[str, bool]:
    """
    Masks sensitive Personally Identifiable Information (PII) including
    credit card numbers, US phone numbers, and Social Security Numbers.
    Returns (sanitized_text, has_masked_pii).
    """
    masked = False

    # Card numbers are checked first: a 16-digit card would otherwise be partially
    # consumed by the phone pattern.
    if CREDIT_CARD_REGEX.search(text):
        text = CREDIT_CARD_REGEX.sub("[REDACTED_CREDIT_CARD]", text)
        masked = True

    if PHONE_REGEX.search(text):
        text = PHONE_REGEX.sub("[REDACTED_PHONE_NUMBER]", text)
        masked = True

    if SSN_REGEX.search(text):
        text = SSN_REGEX.sub("[REDACTED_SSN]", text)
        masked = True

    return text, masked


def detect_prompt_injection(text: str) -> bool:
    """True if ``text`` contains a known injection signature (or was already neutralised)."""
    return INJECTION_PLACEHOLDER in text or any(p.search(text) for p in PROMPT_INJECTION_PATTERNS)


def sanitize_user_input(user_input: str) -> str:
    """
    Neutralises injection phrases and masks PII, returning plain text.

    Use this for routing, embeddings, caching and persistence. Do NOT embed the
    delimiter-wrapped form from ``sanitize_and_isolate_prompt``: the shared wrapper
    tokens would inflate cosine similarity between unrelated queries.
    """
    cleaned_input = user_input
    for pattern in PROMPT_INJECTION_PATTERNS:
        cleaned_input = pattern.sub(INJECTION_PLACEHOLDER, cleaned_input)
    cleaned_input, _ = mask_pii(cleaned_input)
    return cleaned_input.strip()


def sanitize_and_isolate_prompt(user_input: str) -> str:
    """
    Sanitises input and wraps it in strict delimitation tags so it can be embedded in
    an LLM prompt without escaping the system instructions.
    """
    return f"### USER_INPUT ###\n{sanitize_user_input(user_input)}\n### END_USER_INPUT ###"
