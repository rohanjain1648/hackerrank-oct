"""Does the request text authorise deletion?

Two layers:

* ``RuleClassifier`` - deterministic, multilingual and negation-aware. It
  looks for an erasure intent (delete / erase / close account / suppression /
  eliminación / exclusão / 削除 / 삭제 ...) and for signals that the person
  wants something else (access copy, portability, correction, marketing
  opt-out, login help) or explicitly keeps the account ("not asking for
  deletion", "don't delete", "leave the account active").
* An optional LLM classifier (see ``llm.py``) that returns the same label with
  a rationale. When both are available the agent records disagreements and
  prefers the LLM only when its confidence is high; the rule layer is the
  tie-breaker and the offline fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DELETE_PATTERNS = [
    r"\bdelet(e|ed|ing|ion)\b", r"\berase(d|ure)?\b", r"\berasing\b",
    r"\bremov(e|ed|ing|al)\b.*\b(account|profile|personal|data|details|records?|result|entry|copy|traces?|file)\b",
    r"\b(account|profile|personal|data|details|records?|result|entry|copy)\b.*\bremov(e|ed|ing|al)\b",
    # follow-ups on an earlier erasure ("confirm the profile is gone", "my request failed")
    r"\b(is|are|was|were|been) (gone|removed|deleted|erased)\b", r"\bprofile is gone\b", r"\bstill exist(s|ed)?\b",
    r"\bcannot identify me\b", r"\bno longer (point|be linked) (back )?to me\b", r"\bmy (deletion )?request failed\b",
    r"\bclose(d)? (my |this |the )?account\b", r"\bclos(e|ing) .*account\b", r"\bdeleted-account\b",
    r"\bpermanent(ly)? (account[- ])?delet", r"\baccount[- ]deletion\b", r"\bno longer (use|need) this account\b",
    # Spanish / Portuguese / French / Italian / German
    r"\belimina(r|ción|cion)\b", r"\bborra(r|do)\b", r"\bexclus(ã|a)o\b", r"\bexclu(ir|a)\b", r"\bapagar\b",
    r"\bsuppression\b", r"\bsupprim(er|ez)\b", r"\beffacer\b", r"\bcancella(re|zione)\b", r"\blösch(en|ung)\b",
    # Japanese / Korean / Chinese
    r"削除", r"消去", r"삭제", r"탈퇴", r"删除", r"注销",
]
NON_DELETION_PATTERNS = {
    "ACCESS": [r"\bwhat (personal )?information you hold\b", r"\bcopy of (the )?(information|data)\b",
               r"\binformation request\b", r"\bsend me a copy\b", r"\bsubject access\b"],
    "PORTABILITY": [r"\bdownload my (account )?(information|data)\b", r"\bportable format\b", r"\bdata portability\b"],
    "CORRECTION": [r"\bmisspelled\b", r"\bcorrect (it|my|the)\b", r"\bout of date\b", r"\breplace it with\b",
                   r"\bchange the (email|phone|address|name)\b", r"\bupdate (my|the) (email|phone|address|name)\b"],
    "MARKETING_OPT_OUT": [r"\bmarketing lists?\b", r"\bpromotional emails?\b", r"\bunsubscribe\b", r"\bnewsletter\b"],
    "ACCOUNT_SUPPORT": [r"\breset(ting)? the login\b", r"\bcan'?t access my profile\b", r"\bpassword reset\b"],
    "RECORDS_COPY": [r"\bcopy of last year'?s invoices\b", r"\binvoices? for my records\b"],
}
KEEP_PATTERNS = [
    r"\bnot asking for (deletion|erasure)\b", r"\bnot a request to (erase|delete)\b", r"\b(don'?t|do not) (delete|close|erase|remove)\b",
    r"\bleave (the|my) (account|profile)\b", r"\bkeep the account (as it is|active|open)\b", r"\bleave the account active\b",
    r"\bcontinue using the service\b", r"\bstill (need|want|use) (the|this|my)? ?(account|it|receipts)\b",
    r"\bwithout removing\b", r"\bonly\b.*\bmarketing\b|\bmarketing lists only\b",
]


@dataclass
class IntentResult:
    is_deletion: bool
    label: str
    confidence: float
    rationale: str
    source: str = "rules"


class RuleClassifier:
    def classify(self, text: str) -> IntentResult:
        t = (text or "").strip()
        low = t.lower()
        deletion_hits = [p for p in DELETE_PATTERNS if re.search(p, low)]
        keep_hits = [p for p in KEEP_PATTERNS if re.search(p, low)]
        other = {label: [p for p in pats if re.search(p, low)] for label, pats in NON_DELETION_PATTERNS.items()}
        other = {k: v for k, v in other.items() if v}
        if keep_hits or (other and not self._strong_deletion(low)):
            label = next(iter(other), "OTHER_REQUEST")
            why = f"non-deletion intent ({label}); keep-account cues={len(keep_hits)}"
            return IntentResult(False, label, 0.9 if keep_hits else 0.75, why)
        if deletion_hits:
            return IntentResult(True, "DELETION", 0.95, f"erasure intent matched {len(deletion_hits)} pattern(s)")
        return IntentResult(False, "OTHER_REQUEST", 0.5, "no erasure intent found")

    @staticmethod
    def _strong_deletion(low: str) -> bool:
        return bool(re.search(r"\b(delete|erase|close) (my|this|the) account\b|account[- ]deletion|permanent(ly)? delet", low))
