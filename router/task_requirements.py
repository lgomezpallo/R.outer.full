"""Conservative, offline interpretation of task requirements.

This is advisory diagnostics only. It never authorizes a provider/model,
modifies RouteRequest, or triggers inference, discovery, probes, or HTTP.
Caller-supplied requirements and task-derived hints are deliberately separate.
"""
from __future__ import annotations

import re

from .types import Capability

KNOWN_CAPABILITIES = frozenset(Capability.__args__)
ALIASES = {
    "coding": "code",
    "programacion": "code",
    "programación": "code",
    "razonamiento": "reasoning",
    "visión": "vision",
    "imagen": "vision",
    "documentos": "document",
    "documento": "document",
    "transcripción": "transcription",
    "transcripcion": "transcription",
    "voz": "speech",
    "herramientas": "tools",
    "resumen": "summarization",
    "contexto largo": "long_context",
}
# Suggestions are never promoted to hard requirements. Restrict detection to
# action phrases; merely mentioning "Python", "PDF", "JSON" etc. is not enough.
HINT_RULES = (
    ("code", "code_request", r"\b(?:escrib[eií]|program[aá]|implement[aá]|depur[aá]|corrig[eií]|write|implement|debug|fix)\b[^.!?\n]{0,90}\b(?:c[oó]digo|python|javascript|typescript|funci[oó]n|function|script|bug)\b"),
    ("code", "code_request_reversed", r"\b(?:c[oó]digo|python|javascript|typescript|funci[oó]n|function|script)\b[^.!?\n]{0,60}\b(?:escrib[eií]|implement[aá]|write|debug|fix)\b"),
# Common imperative requests omitted by the original code detector.
    ("code", "code_creation_request", r"\b(?:gener[aá]|cre[aá]|arm[aá]|desarroll[aá]|haceme|create|generate|build)\b[^.!?\n]{0,90}\b(?:c[oó]digo|python|javascript|typescript|funci[oó]n|function|script)\b"),
    ("code", "code_repair_request", r"\b(?:correg[íi]|arregl[aá]|revis[aá]|repair|review)\b[^.!?\n]{0,90}\b(?:c[oó]digo|funci[oó]n|function|script|bug)\b"),
    # Creating/editing an image differs from asking a vision model to read it.
    # These remain advisory hints; they cannot authorize a specialist-only
    # model when the caller explicitly requires chat, nor bypass billing.
    ("image_generation", "image_creation_request", r"\b(?:gener[aá]|cre[aá]|dibuj[aá]|renderiz[aá]|generate|create|draw|render)\b[^.!?\n]{0,70}\b(?:una? imagen|una? ilustraci[oó]n|un dibujo|an image|a picture|an illustration)\b"),
    ("image_editing", "image_edit_request", r"\b(?:edit[aá]|modific[aá]|retoc[aá]|edit|modify|retouch)\b[^.!?\n]{0,70}\b(?:esta imagen|la foto|this image|the photo|the picture)\b"),
    ("vision", "visual_request", r"\b(?:analiz[aá]|describ[eií]|interpret[aá]|analy[sz]e|describe|read)\b[^.!?\n]{0,60}\b(?:esta imagen|la foto|this image|the photo|the picture)\b"),
    ("document", "document_request", r"\b(?:resum[eií]|analiz[aá]|extra[eié]|summari[sz]e|analy[sz]e|extract)\b[^.!?\n]{0,70}\b(?:este pdf|el documento|archivo adjunto|this pdf|the document|attached file)\b"),
    ("json", "structured_output", r"\b(?:respond[ée]|devolv[ée]|entreg[aá]|return|output|reply)\b[^.!?\n]{0,60}\bjson\b"),
    ("reasoning", "explicit_reasoning", r"\b(?:razon[aá]|demostr[aá]|resolv[ée]|reason|prove|solve)\b[^.!?\n]{0,60}\b(?:paso a paso|step by step|teorema|theorem|problema matem[aá]tico|math problem)\b"),
    ("transcription", "transcription_request", r"\b(?:transcrib[eií]|transcribe)\b[^.!?\n]{0,60}\b(?:audio|grabaci[oó]n|voz|recording|speech)\b"),
    ("speech", "speech_request", r"\b(?:gener[aá]|sintetiz[aá]|convert[íi]|generate|synthesi[sz]e|convert)\b[^.!?\n]{0,60}\b(?:audio|voz|speech|voice|text.to.speech)\b"),
    ("tools", "tool_request", r"\b(?:us[aá]|utiliz[aá]|call|use)\b[^.!?\n]{0,60}\b(?:herramientas|tools|function calls)\b"),
    ("long_context", "explicit_long_context", r"\b(?:contexto largo|long context|ventana de contexto extensa)\b"),
    ("summarization", "summary_request", r"\b(?:resum[eií]|summari[sz]e)\b[^.!?\n]{0,90}\b(?:texto|art[ií]culo|documento|text|article|document)\b"),
)
LANGUAGE_RULE = re.compile(r"\b(?:respond[ée]|escrib[eií]|contest[aá]|reply|answer|write)\b[^.!?\n]{0,55}\b(?:en espa[nñ]ol|in spanish)\b", re.I)
FENCED = re.compile(r"```[\s\S]*?```", re.M)
QUOTED_LINE = re.compile(r"(?m)^\s*>.*$")


def _normalize_capability(value: str) -> str | None:
    name = " ".join(value.casefold().strip().split())
    name = ALIASES.get(name, name)
    return name if name in KNOWN_CAPABILITIES else None


def analyze_task(
    task: str,
    *,
    requirements: tuple[str, ...] = (),
    required_capabilities: frozenset[str] = frozenset({"chat"}),
    images_present: bool = False,
) -> dict:
    """Produce explainable suggestions; no routing or billing decisions.

    Unknown explicit capabilities are surfaced as invalid, never ignored.
    User text is untrusted: patterns only suggest, never grant capabilities.
    """
    explicit = set()
    rejected = []
    for value in sorted(required_capabilities):
        canonical = _normalize_capability(value)
        if canonical is None:
            rejected.append(value)
        else:
            explicit.add(canonical)

    unparsed = []
    for value in requirements:
        canonical = _normalize_capability(value)
        if canonical is None:
            unparsed.append(value[:120])
        else:
            explicit.add(canonical)

    if images_present:
        explicit.update(("chat", "vision"))

    # Do not interpret code fences, quoted replies, or contextual documents
    # as user instructions. The task itself is the only heuristic source.
    clean = QUOTED_LINE.sub("", FENCED.sub("", task[:100000]))
    suggestions: dict[str, set[str]] = {}
    for capability, rule, expression in HINT_RULES:
        if re.search(expression, clean, re.I):
            suggestions.setdefault(capability, set()).add(rule)

    language = "spanish_requested" if LANGUAGE_RULE.search(clean) else "unspecified"
    return {
        "explicit_required_capabilities": sorted(explicit),
        "invalid_explicit_capabilities": sorted(set(rejected)),
        "unparsed_requirements": unparsed,
        "suggested_capabilities": [
            {"capability": cap, "rule_ids": sorted(rules), "confidence": "heuristic_only"}
            for cap, rules in sorted(suggestions.items())
        ],
        "language_preference": language,
        "images_present": bool(images_present),
        "routing_modified": False,
        "inference_performed": False,
        "account_billing_verified": False,
        "suggestions_are_not_authorization": True,
    }
