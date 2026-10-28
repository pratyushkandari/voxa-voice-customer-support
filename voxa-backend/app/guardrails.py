"""
Ingress guardrails: PII masking and prompt-injection neutralisation.

These run on every user utterance before it is routed, embedded, cached or persisted,
so raw card numbers, phone numbers, emails and SSNs never reach Redis, PostgreSQL, the
worker fleet or Gemini.
"""
import re
from typing import Tuple

# Regex patterns for sensitive PII
EMAIL_REGEX = re.compile(r'\b[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b')
CREDIT_CARD_REGEX = re.compile(r'\b(?:\d[ -]*?){13,16}\b')
# An optional country code, then a 10-digit number written the US way (555-123-4567,
# (555) 123-4567) or the Indian way (98765 43210).
PHONE_REGEX = re.compile(
    r'(?<!\w)(?:\+?\d{1,3}[-.\s]?)?'
    r'(?:\(\d{3}\)\s?\d{3}[-.\s]?\d{4}|\d{3}[-.\s]?\d{3}[-.\s]?\d{4}|\d{5}[-.\s]?\d{5})(?!\w)'
)
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
    Masks sensitive Personally Identifiable Information (PII): email addresses, credit
    card numbers, phone numbers and US Social Security Numbers.
    Returns (sanitized_text, has_masked_pii).
    """
    masked = False

    # Emails first, so digits inside an address are not half-masked as a phone number.
    if EMAIL_REGEX.search(text):
        text = EMAIL_REGEX.sub("[REDACTED_EMAIL]", text)
        masked = True

    # Card numbers before phones: a 16-digit card would otherwise be partially
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
    Neutralises injection phrases and masks PII, returning plain text. Used for routing,
    embeddings, caching and persistence. Idempotent: sanitising twice changes nothing.
    """
    cleaned_input = user_input
    for pattern in PROMPT_INJECTION_PATTERNS:
        cleaned_input = pattern.sub(INJECTION_PLACEHOLDER, cleaned_input)
    cleaned_input, _ = mask_pii(cleaned_input)
    return cleaned_input.strip()
