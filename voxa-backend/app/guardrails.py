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

def mask_pii(text: str) -> Tuple[str, bool]:
    """
    Masks sensitive Personally Identifiable Information (PII) including
    credit card numbers, US phone numbers, and Social Security Numbers.
    Returns (sanitized_text, has_masked_pii).
    """
    masked = False
    
    # Mask Credit Cards
    if CREDIT_CARD_REGEX.search(text):
        text = CREDIT_CARD_REGEX.sub("[REDACTED_CREDIT_CARD]", text)
        masked = True
        
    # Mask Phone Numbers
    if PHONE_REGEX.search(text):
        text = PHONE_REGEX.sub("[REDACTED_PHONE_NUMBER]", text)
        masked = True
        
    # Mask SSN
    if SSN_REGEX.search(text):
        text = SSN_REGEX.sub("[REDACTED_SSN]", text)
        masked = True
        
    return text, masked

def sanitize_and_isolate_prompt(user_input: str) -> str:
    """
    Strips dangerous prompt injection attempts and wraps user input in strict
    delimitation tags to prevent LLM system prompt escaping.
    """
    cleaned_input = user_input
    for pattern in PROMPT_INJECTION_PATTERNS:
        cleaned_input = pattern.sub("[BLOCKED_INSTRUCTION_OVERRIDE]", cleaned_input)
        
    # Apply PII filter
    cleaned_input, _ = mask_pii(cleaned_input)
    
    # Wrap in strict isolation block
    isolated_prompt = f"### USER_INPUT ###\n{cleaned_input.strip()}\n### END_USER_INPUT ###"
    return isolated_prompt
