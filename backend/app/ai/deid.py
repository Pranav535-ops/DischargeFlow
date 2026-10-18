"""De-identification before prompting, re-identification after.

Direct identifiers (names, the attending physician, room numbers, dates) are swapped for stable tokens such as
[PATIENT_1] before any text reaches a model, and the tokens are swapped back in the model's output. Contact details
that slip into free text (phone numbers, emails, MRN-like numbers) are redacted outright and never restored.
Only the fields a prompt needs are sent at all; see ai/service.py:summary_facts.
"""
import re
from datetime import date

# Redacted outright: these have no business in a prompt, so there is nothing to restore.
SCRUBBERS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)"), "[PHONE]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(?:MRN|mrn)[:#\s]*\w+"), "[MRN]"),
    # Dates other than a bare year are identifiers under HIPAA Safe Harbor.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "[DATE]"),
]
MIN_PART_LENGTH = 3  # shorter name parts ("Al", initials) would match inside ordinary words
TITLES = {"Dr", "Mr", "Mrs", "Ms", "Miss", "Prof"}
AMBIGUOUS = "[AMBIGUOUS_NAME]"


class Deidentifier:
    """Maps identifier values to tokens for one request. Longest values are replaced first.

    A value shared by two different people (two patients both called "John Smith", or the same surname) can't be
    mapped back reliably, so it is redacted to [AMBIGUOUS_NAME] and never restored, instead of being left in clear.
    """

    def __init__(self) -> None:
        self._value_for: dict[str, str] = {}  # token -> display value
        self._allocated: dict[tuple[str, str], str] = {}  # (kind, identity) -> token
        self._counters: dict[str, int] = {}
        # Full values match case-insensitively. Single name parts ("Lovelace") match only when capitalized, so a
        # patient called Will never turns the verb "will" into a token.
        self._full: dict[str, str] = {}
        self._parts: dict[str, str] = {}

    @staticmethod
    def _register(table: dict[str, str], key: str, tok: str) -> None:
        existing = table.get(key)
        if existing is None:
            table[key] = tok
        elif existing != tok:
            table[key] = AMBIGUOUS

    def token(self, kind: str, value: str | None, identity: str | None = None) -> str:
        """The token for `value` (allocating one), e.g. token("PATIENT", "Ada Lovelace") -> "[PATIENT_1]".

        `identity` distinguishes different people who share a value (pass the record id).
        """
        if not value or not value.strip():
            return f"[{kind}_UNKNOWN]"
        value = value.strip()
        alloc_key = (kind, identity or value.lower())
        if alloc_key in self._allocated:
            return self._allocated[alloc_key]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        tok = f"[{kind}_{self._counters[kind]}]"
        self._allocated[alloc_key] = tok
        self._value_for[tok] = value
        self._register(self._full, value.lower(), tok)
        return tok

    def add_person(self, kind: str, full_name: str | None, identity: str | None = None,
                   match_parts: bool = True) -> str:
        """Token for a person, also matching their individual name parts."""
        tok = self.token(kind, full_name, identity)
        if match_parts:
            for part in re.split(r"[\s,]+", (full_name or "").strip()):
                part = part.strip(".").capitalize()
                if len(part) >= MIN_PART_LENGTH and part not in TITLES:
                    self._register(self._parts, part, tok)
        return tok

    def value(self, token: str) -> str | None:
        return self._value_for.get(token)

    def tokens(self) -> dict[str, str]:
        return dict(self._value_for)

    def redact(self, text: str | None) -> str:
        if not text:
            return ""
        for pattern, replacement in SCRUBBERS:
            text = pattern.sub(replacement, text)
        for key in sorted(self._full, key=len, reverse=True):
            text = re.sub(rf"(?<![\w\[]){re.escape(key)}(?![\w\]])", self._full[key], text, flags=re.I)
        for part in sorted(self._parts, key=len, reverse=True):
            text = re.sub(rf"(?<![\w\[]){re.escape(part)}(?![\w\]])", self._parts[part], text)
        return text

    def reidentify(self, text: str) -> str:
        return re.sub(r"\[[A-Z_]+_\d+\]", lambda m: self._value_for.get(m.group(0), m.group(0)), text)

    def reidentify_obj(self, obj):
        """Re-identify every string inside a JSON-like structure."""
        if isinstance(obj, str):
            return self.reidentify(obj)
        if isinstance(obj, list):
            return [self.reidentify_obj(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.reidentify_obj(v) for k, v in obj.items()}
        return obj


def age_from_dob(dob: str | None, today: date | None = None) -> int | None:
    """Age in whole years; ages over 89 are reported as 90 (HIPAA Safe Harbor)."""
    try:
        born = date.fromisoformat((dob or "")[:10])
    except ValueError:
        return None
    today = today or date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return min(age, 90) if age >= 0 else None


def days_between(start: str | None, end: date | None = None) -> int | None:
    try:
        begun = date.fromisoformat((start or "")[:10])
    except ValueError:
        return None
    return max(((end or date.today()) - begun).days, 0)
