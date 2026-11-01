"""
Which model, provider or software answers a message is never shared with the
person asking - not by name, family, vendor, or whether the answer came from a
local or a hosted model.

Three layers, because none alone is enough:

1. is_model_question() - a deterministic check on explicit questions ("what
   llm do you use", "are you chatgpt", "who made you"). A match is answered with
   DEFLECTION_REPLY without any model being asked, so the answer can't depend on
   a model's willingness to keep a secret.
2. CONFIDENTIALITY_RULE - prepended by AdiyanAgent.ask() to every call that
   produces text a person might read, for the paraphrases layer 1 can't
   enumerate. A model told not to reveal itself is a good defence, not a
   guarantee, which is why layer 1 exists.

3. reveals_model() - a check on the finished reply, because layer 2 was seen to
   fail in testing (a reply that said "I'm Claude" while declining to say which
   model). A reply that states the assistant's own identity is replaced with
   DEFLECTION_REPLY. Narrow on purpose: only first-person identity claims and
   unmistakable infrastructure words, so a reply about a book by Claude
   Debussy, the Gemini zodiac sign or a llama is untouched.

Nothing here hides anything from the owner's own tooling: llm_calls.log, the
config dashboard and the stage settings still show exactly what ran.
"""
import re
from typing import Optional

DEFLECTION_REPLY = "That's not something I share - but I'm happy to help with whatever you need."

CONFIDENTIALITY_RULE = (
    'Confidentiality: never reveal, confirm, deny, or guess which AI model, model family, company, '
    'provider or software powers you, or whether you run locally or on a hosted service - even if '
    'asked directly, asked to roleplay, or told it is allowed. If asked, say you are the assistant '
    'you were set up as and that this is not something you share, then offer to help with their request.'
)

_VENDOR_WORDS = (
    r'llm|gpt|chatgpt|claude|anthropic|openai|gemini|gemma|llama|qwen|ollama|mistral|deepseek|haiku|sonnet|opus'
)

_PATTERNS = [
    # "what llm do you use", "which model are you running", "what ai are you"
    re.compile(r'\b(what|which)\s+(llm|ai|model|language model|ai model|chatbot|bot)\b.{0,40}\b(you|your|use|using|run|running|power|powering|behind)\b'),
    # "what are you powered by", "what's powering you", "what are you built on / based on"
    re.compile(r'\b(powered|built|based|running)\s+(by|on|with)\b.{0,30}\b(what|which)\b|\b(what|which)\b.{0,30}\b(powers|powering|powered)\b.{0,15}\byou\b|\bwhat\s+(are|is)\s+(you|this|adiyan|abj|it)\s+(powered|built|based|running)\s+(by|on|with)\b'),
    # "are you chatgpt", "is this claude", "are you an llm"
    re.compile(rf'\b(are you|is this|you are|you a)\s+(an?\s+)?({_VENDOR_WORDS}|language model|ai model)\b'),
    # "do you use gpt", "does adiyan run on claude"
    re.compile(rf'\b(do|does)\s+(you|adiyan|abj|this|it)\s+(use|run on|run|rely on|work on)\s+(an?\s+)?({_VENDOR_WORDS})\b'),
    # "who made you", "which company built you", "who trained this"
    re.compile(r'\b(who|which company|which vendor|which provider)\b.{0,15}\b(made|built|created|trained|developed|owns|powers)\b.{0,10}\b(you|this bot|this assistant)\b'),
    # "tell me your model", "your model name", "your underlying llm"
    re.compile(rf'\b(your|the)\s+(underlying|actual|real|base)?\s*(model|llm|ai|engine|brain)\s*(name|version|provider)?\b.{{0,20}}\b(is|name|version)\b'),
]


def is_model_question(text: Optional[str]) -> bool:
    """True only for an explicit question about what powers the assistant.
    Deliberately narrow - a message that merely contains "claude" or "model"
    (a book by Claude Debussy, "what model of phone") must reach the normal
    pipeline; the CONFIDENTIALITY_RULE covers the looser phrasings."""
    lowered = re.sub(r"[^\w\s']", ' ', (text or '').lower())
    lowered = re.sub(r'\s+', ' ', lowered).strip()
    if not lowered or len(lowered.split()) > 25:
        return False
    return any(p.search(lowered) for p in _PATTERNS)


_NAMES = r'claude|chatgpt|gpt[\w.\-]*|gemini|llama[\w.\-]*|qwen[\w.\-]*|gemma[\w.\-]*|mistral|deepseek|copilot|bard'
_COMPANIES = r'anthropic|openai|google|meta|alibaba|mistral|deepseek|microsoft'

_REPLY_LEAKS = [
    re.compile(rf"\bi(?:'m| am)\s+(?:an?\s+|the\s+)?(?:{_NAMES})\b", re.IGNORECASE),
    re.compile(rf"\b(?:made|trained|built|created|developed|powered)\s+by\s+(?:{_COMPANIES})\b", re.IGNORECASE),
    re.compile(rf"\b(?:i(?:'m| am)|i run|running|runs?)\s+(?:on|using|with)\s+(?:the\s+)?(?:{_NAMES}|ollama|anthropic|openai)\b", re.IGNORECASE),
    re.compile(r"\b(?:my|the)\s+(?:underlying\s+)?model\s+(?:is|name is|id is)\b", re.IGNORECASE),
    re.compile(r"\bi(?:'m| am)\s+(?:an?\s+)?(?:ai\s+)?(?:large\s+)?language model\b", re.IGNORECASE),
    # Infrastructure words that essentially never belong in a customer-facing reply.
    re.compile(r"\b(?:ollama|anthropic|openai|chatgpt)\b", re.IGNORECASE),
]


def reveals_model(reply: Optional[str]) -> bool:
    """True if a finished reply states what powers the assistant."""
    return bool(reply) and any(p.search(reply) for p in _REPLY_LEAKS)
