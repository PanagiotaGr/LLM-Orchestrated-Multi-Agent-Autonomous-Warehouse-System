"""Natural-language command parser.

Three modes, picked at runtime based on env vars:

1. **anthropic** — uses Claude via the `anthropic` SDK (if ANTHROPIC_API_KEY).
2. **openai** — uses OpenAI's Chat Completions API (if OPENAI_API_KEY).
3. **local** — deterministic rule-based parser (always available).

The local parser is the fallback whenever an API call fails, the env vars
are missing, or LLM_PROVIDER=local is set explicitly. This keeps the demo
fully runnable offline — a hard requirement for portfolio / classroom use.

The parser output is always a `ParsedIntent` (see models.py). The caller
should treat `action == "unknown"` as a parse failure.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .models import ParsedIntent, Priority

log = logging.getLogger("llm_parser")


# ─── Public entry-point ─────────────────────────────────────────────────────
async def parse_command(text: str) -> ParsedIntent:
    """Parse `text` into a structured `ParsedIntent`.

    Picks the best available backend and falls back to the rule-based parser
    on any failure.
    """
    provider = os.getenv("LLM_PROVIDER", "auto").lower()

    if provider in ("auto", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
        try:
            return await _parse_with_anthropic(text)
        except Exception as e:
            log.warning("Anthropic parse failed (%s); falling back to local.", e)

    if provider in ("auto", "openai") and os.getenv("OPENAI_API_KEY"):
        try:
            return await _parse_with_openai(text)
        except Exception as e:
            log.warning("OpenAI parse failed (%s); falling back to local.", e)

    return parse_local(text)


# ─── Rule-based local parser ────────────────────────────────────────────────
# Maps loose user-language to canonical location IDs the warehouse knows.
_ZONE_PATTERNS = {
    "zone_a": [r"\bzone\s*a\b", r"\barea\s*a\b"],
    "zone_b": [r"\bzone\s*b\b", r"\barea\s*b\b"],
    "zone_c": [r"\bzone\s*c\b", r"\barea\s*c\b"],
    "zone_d": [r"\bzone\s*d\b", r"\barea\s*d\b"],
    "zone_e": [r"\bzone\s*e\b", r"\barea\s*e\b"],
    "storage": [r"\bstorage\b", r"\bdepot\b", r"\bwarehouse\b"],
}

_PRIORITY_PATTERNS = {
    Priority.URGENT: [r"\burgent", r"\bcritical", r"\bemergency", r"\basap", r"\bimmediat"],
    Priority.HIGH: [r"\bhigh\s*priority", r"\bquickly", r"\bfast", r"\bprioritize", r"\bpriority"],
    Priority.LOW: [r"\blow\s*priority", r"\bwhenever", r"\beventually"],
}


def parse_local(text: str) -> ParsedIntent:
    """Deterministic rule-based parser. Never raises."""
    t = text.lower().strip()

    # ── Robot failure ────────────────────────────────────────────────────
    fail_match = re.search(
        r"(?:simulate|trigger|cause).{0,20}(?:robot[_\s-]*)?(\d+).{0,20}(?:failure|fail|crash|break)",
        t,
    )
    if not fail_match:
        fail_match = re.search(
            r"(?:fail|crash|break|disable|kill).{0,20}robot[_\s-]*(\d+)",
            t,
        )
    if fail_match:
        rid = f"robot_{fail_match.group(1)}"
        return ParsedIntent(
            action="fail_robot",
            robot_id=rid,
            message=f"Interpreted as: fail {rid}.",
        )

    # ── Reassign unfinished ──────────────────────────────────────────────
    if re.search(r"reassign|redistribute|re-?route", t) and re.search(
        r"unfinish|pending|stuck|stalled|orphan|task", t
    ):
        return ParsedIntent(
            action="reassign_unfinished",
            message="Interpreted as: reassign all unfinished tasks.",
        )

    # ── Recharge low-battery robots ──────────────────────────────────────
    if re.search(r"recharge|charging|charge.*battery|low.*battery", t):
        return ParsedIntent(
            action="recharge_low_battery",
            message="Interpreted as: send low-battery robots to charge.",
        )

    # ── Add obstacle ─────────────────────────────────────────────────────
    if re.search(r"add.*obstacle|drop.*obstacle|place.*obstacle|spawn.*obstacle|block.*aisle", t):
        return ParsedIntent(
            action="add_obstacle",
            message="Interpreted as: add a random obstacle to the floor.",
        )

    # ── Reset ────────────────────────────────────────────────────────────
    if re.search(r"^reset\b|\bclear\s+(all|everything)|\bstart\s+over\b", t):
        return ParsedIntent(action="reset", message="Interpreted as: full reset.")

    # ── Task creation ────────────────────────────────────────────────────
    dropoff = _detect_location(t, exclude=("storage",))
    if dropoff is None and re.search(r"\bdeliver|\bsend|\bmove|\bbring|\btransport", t):
        # Re-scan including storage just in case (e.g. "move from C to storage")
        dropoff = _detect_location(t)

    if dropoff:
        package = _detect_package(t)
        priority = _detect_priority(t)
        pickup = _detect_pickup(t) or "storage"

        return ParsedIntent(
            action="create_task",
            package_id=package,
            pickup_location=pickup,
            dropoff_location=dropoff,
            priority=priority,
            message=(
                f"Interpreted as: deliver {package or 'a package'} "
                f"from {pickup} → {dropoff} ({priority.value})."
            ),
        )

    return ParsedIntent(
        action="unknown",
        message=(
            "Sorry, I couldn't parse that command. Try: "
            "'Deliver package P3 to Zone B urgently', "
            "'Fail robot 1', or 'Recharge low battery robots'."
        ),
    )


def _detect_location(t: str, exclude: tuple[str, ...] = ()) -> str | None:
    for loc, patterns in _ZONE_PATTERNS.items():
        if loc in exclude:
            continue
        if any(re.search(p, t) for p in patterns):
            return loc
    return None


def _detect_pickup(t: str) -> str | None:
    """Try to detect the *source* of a delivery (vs. the destination)."""
    m = re.search(
        r"from\s+(zone\s*[a-e]|storage|depot|warehouse)",
        t,
    )
    if not m:
        return None
    raw = m.group(1).replace(" ", "_")
    if raw in ("depot", "warehouse"):
        return "storage"
    if raw.startswith("zone_") or raw == "storage":
        return raw
    return None


def _detect_package(t: str) -> str | None:
    m = re.search(r"\b(?:package|item|pkg|parcel)\s*[#]?\s*([a-z0-9]{1,8})", t)
    if m:
        return m.group(1).upper()
    # Also catch bare "P3" / "A1" style identifiers.
    m = re.search(r"\b([pP]\d+|[A-Z]\d+)\b", t)
    if m:
        return m.group(1).upper()
    return None


def _detect_priority(t: str) -> Priority:
    for p, patterns in _PRIORITY_PATTERNS.items():
        if any(re.search(pat, t) for pat in patterns):
            return p
    return Priority.NORMAL


# ─── LLM backends ──────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You translate natural-language warehouse operator commands into JSON.

Return ONLY a JSON object with these fields:
{
  "action": one of
      "create_task" | "fail_robot" | "recharge_low_battery" |
      "reassign_unfinished" | "add_obstacle" | "reset" | "unknown",
  "package_id":      string or null,
  "pickup_location": "storage" | "zone_a"..."zone_e" or null,
  "dropoff_location":"storage" | "zone_a"..."zone_e" or null,
  "priority":        "low" | "normal" | "high" | "urgent",
  "robot_id":        "robot_0", "robot_1", ... or null,
  "message":         a one-sentence human-readable explanation of how you parsed the command
}

Rules:
- "Deliver X to Zone Y" -> action=create_task, pickup_location=storage (unless stated),
  dropoff_location=zone_y.
- "Send robot N to fail" or "fail robot N" -> action=fail_robot, robot_id=robot_N.
- "Recharge low-battery robots" -> action=recharge_low_battery.
- "Reassign unfinished/orphan tasks" -> action=reassign_unfinished.
- "Add an obstacle" or "block an aisle" -> action=add_obstacle.
- "Reset" or "start over" -> action=reset.
- If you cannot determine the action, return action="unknown".
- "urgent" / "ASAP" / "immediately" => priority="urgent".
- "quickly" / "fast" / "prioritize" => priority="high".
Do NOT include any text outside the JSON object.
"""


async def _parse_with_anthropic(text: str) -> ParsedIntent:
    import anthropic  # local import: only needed if the user opted in

    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
    msg = await client.messages.create(
        model=model,
        max_tokens=400,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _coerce_intent(raw, text, source="anthropic")


async def _parse_with_openai(text: str) -> ParsedIntent:
    from openai import AsyncOpenAI  # local import

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=400,
    )
    raw = resp.choices[0].message.content or ""
    return _coerce_intent(raw, text, source="openai")


def _coerce_intent(raw: str, original: str, source: str) -> ParsedIntent:
    """Best-effort JSON extraction → ParsedIntent.

    Models sometimes wrap JSON in code fences or add a sentence of preamble.
    """
    # Strip code fences.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    # Take the first {...} block we can find.
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        log.warning("[%s] No JSON object found in response: %r", source, raw)
        return parse_local(original)

    try:
        data: dict[str, Any] = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log.warning("[%s] JSON parse error %s in: %r", source, e, raw)
        return parse_local(original)

    # Sanity-check fields before constructing the Pydantic model.
    if data.get("action") not in {
        "create_task",
        "fail_robot",
        "recharge_low_battery",
        "reassign_unfinished",
        "add_obstacle",
        "reset",
        "unknown",
    }:
        data["action"] = "unknown"
    data.setdefault("message", f"Parsed by {source}.")

    try:
        return ParsedIntent(**data)
    except Exception as e:
        log.warning("[%s] Pydantic validation failed: %s -- payload=%s", source, e, data)
        return parse_local(original)
