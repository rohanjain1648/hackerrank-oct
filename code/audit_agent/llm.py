"""Claude integration (optional; enabled with --llm and ANTHROPIC_API_KEY).

Two narrow, auditable jobs - the evidence reasoning itself stays deterministic:

1. **Intent classification** of ``request_text`` (multilingual, negation and
   follow-up aware) with a JSON-schema-constrained answer.
2. **Screenshot transcription**: Claude vision returns the screen as text rows
   ("row-by-row, cells separated by ' | '"), the same shape the local OCR
   produces, so both feed the same document interpreter.

Answers are cached on disk keyed by prompt version + input hash, so a re-run
costs nothing and the final run is reproducible. Every call's token usage is
recorded by ``UsageTracker`` for the usage report.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from .intent import IntentResult
from .paths import CACHE_DIR

DEFAULT_MODEL = "claude-opus-5-5"
PROMPT_VERSION = "v1"

INTENT_SYSTEM = """You review privacy requests sent to an online shop.
Decide whether the person is asking for (or following up on) erasure of their
account / personal data. Follow-ups about a deletion that already happened or
failed ("confirm my profile is gone", "the search result still shows me") ARE
deletion requests. Requests for access, a data copy, portability, correction,
marketing opt-out, login help or invoices are NOT, even when they mention the
word "delete" in a negated way ("please don't delete anything").
The text may be in any language."""

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_deletion": {"type": "boolean"},
        "label": {"type": "string", "enum": ["DELETION", "ACCESS", "PORTABILITY", "CORRECTION",
                                              "MARKETING_OPT_OUT", "ACCOUNT_SUPPORT", "RECORDS_COPY",
                                              "OTHER_REQUEST"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["is_deletion", "label", "confidence", "rationale"],
    "additionalProperties": False,
}

VISION_PROMPT = """Transcribe this internal admin screenshot as plain text rows.
Output JSON {"rows": [...]} where each row is one visual line of the screen, top
to bottom, with table cells separated by " | ". Copy identifiers exactly
(resource ids such as account_123, user ids, emails, phone numbers, statuses,
dates). Do not summarise or add anything that is not on the screen."""

VISION_SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {"type": "string"}}},
    "required": ["rows"],
    "additionalProperties": False,
}


class ClaudeClient:
    def __init__(self, usage=None, model: str | None = None):
        self.usage = usage
        self.model = model or os.environ.get("AUDIT_CLAUDE_MODEL", DEFAULT_MODEL)
        self.enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.vision_enabled = self.enabled and os.environ.get("AUDIT_DISABLE_VISION") != "1"
        self.cache_dir = CACHE_DIR / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = None

    def _sdk(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _cached(self, kind: str, payload: str):
        key = hashlib.sha256(f"{PROMPT_VERSION}|{self.model}|{kind}|{payload}".encode()).hexdigest()[:24]
        return self.cache_dir / f"{kind}_{key}.json"

    def _call(self, purpose: str, system: str | None, content, schema: dict, max_tokens: int) -> dict | None:
        import anthropic

        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            # On a safety decline the API retries on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if system:
            kwargs["system"] = system
        try:
            response = self._sdk().beta.messages.create(**kwargs)
        except anthropic.RateLimitError:
            return None
        except anthropic.APIStatusError:
            return None
        except anthropic.APIConnectionError:
            return None
        if self.usage is not None:
            self.usage.record_llm(self.model, response.usage.input_tokens, response.usage.output_tokens, purpose)
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        return json.loads(text) if text else None

    def classify_intent(self, case_id: str, text: str) -> IntentResult | None:
        if not self.enabled:
            return None
        path = self._cached("intent", text)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if self.usage is not None:
                self.usage.record_llm(self.model, 0, 0, "intent classification", cached=True)
        else:
            data = self._call("intent classification", INTENT_SYSTEM,
                              f"Request text:\n<<<\n{text}\n>>>", INTENT_SCHEMA, 1024)
            if data is None:
                return None
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return IntentResult(bool(data["is_deletion"]), data["label"], float(data["confidence"]),
                            data["rationale"], source=f"llm:{self.model}")

    def vision_rows(self, image_path: Path) -> list[str] | None:
        raw = Path(image_path).read_bytes()
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.standard_b64encode(raw).decode("utf-8")}},
            {"type": "text", "text": VISION_PROMPT},
        ]
        data = self._call("screenshot transcription", None, content, VISION_SCHEMA, 8000)
        return data["rows"] if data else None
