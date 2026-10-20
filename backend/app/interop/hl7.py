"""Minimal HL7 v2 (ER7, pipe-delimited) reader and ACK writer for the inbound ADT feed.

Only what the ADT events need: segments, fields, repetitions, components, sub-components and the standard escape
sequences. No HL7 library: the format is small, and one more dependency in the request path isn't worth it.

Transport is HTTP: the hospital's interface engine (Mirth, Rhapsody, ...) receives MLLP from the ADT system and
POSTs each message here, which serverless hosts can serve (they can't hold an MLLP TCP listener). MLLP framing
bytes are tolerated and stripped.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.ids import new_id

# HL7 table 0357 (message error condition codes), the subset this adapter reports.
ERROR_CODES = {
    "100": "Segment sequence error",
    "101": "Required field missing",
    "102": "Data type error",
    "200": "Unsupported message type",
    "201": "Unsupported event code",
    "202": "Unsupported processing id",
    "204": "Unknown key identifier",
    "205": "Duplicate key identifier",
    "207": "Application internal error",
}

ACK_SENDING_APPLICATION = "DischargeFlow"
_STANDARD_ENCODING = "^~\\&"


class HL7Error(Exception):
    """A message this adapter can't accept, reported in the ACK (AR: rejected, AE: application error)."""

    def __init__(self, code: str, text: str, ack: str = "AR"):
        super().__init__(text)
        self.code = code
        self.text = text
        self.ack = ack


@dataclass(frozen=True)
class Encoding:
    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    def unescape(self, value: str) -> str:
        if self.escape not in value:
            return value
        replacements = {"F": self.field, "S": self.component, "R": self.repetition, "E": self.escape,
                        "T": self.subcomponent}
        out, i = [], 0
        while i < len(value):
            if value[i] == self.escape:
                end = value.find(self.escape, i + 1)
                if end > i:
                    seq = value[i + 1:end]
                    # Formatting and hex sequences (\H\, \X0D\, ...) carry nothing an ADT field needs; drop them.
                    out.append(replacements.get(seq, ""))
                    i = end + 1
                    continue
            out.append(value[i])
            i += 1
        return "".join(out)

    def escape_text(self, value: str) -> str:
        value = value.replace(self.escape, f"{self.escape}E{self.escape}")
        for char, code in ((self.field, "F"), (self.component, "S"), (self.repetition, "R"),
                           (self.subcomponent, "T")):
            value = value.replace(char, f"{self.escape}{code}{self.escape}")
        return value.replace("\r", " ").replace("\n", " ")


class Segment:
    def __init__(self, fields: list[str], encoding: Encoding):
        self._fields = fields
        self._enc = encoding
        self.name = fields[0]

    def field(self, n: int) -> str:
        """Raw (still escaped) field n, numbered as in the HL7 spec (MSH-1 is the field separator)."""
        return self._fields[n] if n < len(self._fields) else ""

    def repetitions(self, n: int) -> list[str]:
        raw = self.field(n)
        return raw.split(self._enc.repetition) if raw else []

    def component(self, n: int, c: int = 1, rep: int = 0, sub: int = 1) -> str:
        """Unescaped, stripped value of field n, repetition `rep`, component c, sub-component `sub` (1-based)."""
        reps = self.repetitions(n)
        if rep >= len(reps):
            return ""
        return self.component_of(reps[rep], c, sub)

    def component_of(self, repetition: str, c: int = 1, sub: int = 1) -> str:
        components = repetition.split(self._enc.component)
        if c > len(components):
            return ""
        subs = components[c - 1].split(self._enc.subcomponent)
        if sub > len(subs):
            return ""
        return self._enc.unescape(subs[sub - 1]).strip()


class Message:
    def __init__(self, segments: list[Segment], encoding: Encoding):
        self.segments = segments
        self.encoding = encoding
        self.msh = segments[0]

    def segment(self, name: str) -> Optional[Segment]:
        return next((s for s in self.segments if s.name == name), None)

    @property
    def message_type(self) -> str:
        return self.msh.component(9, 1).upper()

    @property
    def trigger_event(self) -> str:
        return self.msh.component(9, 2).upper()

    @property
    def control_id(self) -> str:
        return self.msh.component(10)

    @property
    def processing_id(self) -> str:
        return self.msh.component(11).upper()

    @property
    def version(self) -> str:
        return self.msh.component(12)


def parse(raw: str) -> Message:
    """Parse an ER7 message. Raises HL7Error (AR) when there is no usable MSH segment."""
    text = raw.strip("\x0b\x1c\r\n ").replace("\r\n", "\r").replace("\n", "\r")
    lines = [line for line in text.split("\r") if line.strip()]
    if not lines or not lines[0].startswith("MSH") or len(lines[0]) < 8:
        raise HL7Error("100", "Message must start with an MSH segment")
    header = lines[0]
    field_sep = header[3]
    encoding_chars = header[4:].split(field_sep, 1)[0]
    if len(encoding_chars) < 4 or field_sep.isalnum():
        raise HL7Error("102", "MSH-1/MSH-2 do not define valid separators")
    enc = Encoding(field=field_sep, component=encoding_chars[0], repetition=encoding_chars[1],
                   escape=encoding_chars[2], subcomponent=encoding_chars[3])

    segments = []
    for i, line in enumerate(lines):
        parts = line.split(field_sep)
        if i == 0:
            parts.insert(1, field_sep)  # so that MSH field n is parts[n], as for every other segment
        if len(parts[0]) != 3:
            raise HL7Error("100", f"Malformed segment at line {i + 1}")
        segments.append(Segment(parts, enc))
    return Message(segments, enc)


def hl7_timestamp(when: Optional[datetime] = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")


def parse_date(value: str) -> Optional[str]:
    """HL7 DT/DTM (YYYYMMDD[HHMM[SS]][+ZZZZ]) to ISO YYYY-MM-DD, or None if it isn't a valid date."""
    digits = value[:8]
    if len(digits) != 8 or not (digits.isascii() and digits.isdigit()):
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def parse_datetime(value: str) -> Optional[datetime]:
    """HL7 DTM (YYYYMMDD[HH[MM[SS[.S]]]][+/-ZZZZ]) to an aware datetime, or None if it isn't valid.

    A time without an offset is taken as UTC: HL7 says it is the sender's local time, which this adapter can't know.
    """
    value = value.strip()
    offset = timezone.utc
    for sign in ("+", "-"):
        if sign in value[8:]:
            value, zone = value.rsplit(sign, 1)
            # ASCII only: str.isdigit() also accepts characters such as "²" that int() refuses.
            if len(zone) != 4 or not (zone.isascii() and zone.isdigit()):
                return None
            hours, minutes = int(zone[:2]), int(zone[2:])
            if hours >= 24 or minutes >= 60:  # timezone() refuses an offset of a day or more
                return None
            total = hours * 60 + minutes
            offset = timezone(timedelta(minutes=total if sign == "+" else -total))
            break
    digits = value.split(".", 1)[0]
    if len(digits) < 8 or len(digits) > 14 or len(digits) % 2 or not (digits.isascii() and digits.isdigit()):
        return None
    try:
        parsed = datetime.strptime(digits.ljust(14, "0"), "%Y%m%d%H%M%S")
        return parsed.replace(tzinfo=offset).astimezone(timezone.utc)
    except (ValueError, OverflowError):  # OverflowError: the offset moves year 1 or 9999 out of range
        return None


def build_ack(message: Optional[Message], ack_code: str, text: str = "", error_code: Optional[str] = None) -> str:
    """An ACK for `message` (or for an unparseable one, when None). Always written with the standard separators."""
    enc = Encoding()

    def from_msh(n: int) -> str:
        return enc.escape_text(message.msh.component(n)) if message else ""

    trigger = enc.escape_text(message.trigger_event) if message else ""
    msh = [
        "MSH", _STANDARD_ENCODING,
        ACK_SENDING_APPLICATION, from_msh(6),  # we answer as the application and facility the message was sent to
        from_msh(3), from_msh(4),
        hl7_timestamp(), "",
        f"ACK^{trigger}^ACK" if trigger else "ACK",
        new_id("ack", 16),
        from_msh(11) or "P",
        from_msh(12) or "2.5.1",
    ]
    original_control = from_msh(10)
    segments = ["|".join(msh), "|".join(["MSA", ack_code, original_control, enc.escape_text(text)])]
    if error_code:
        description = enc.escape_text(ERROR_CODES.get(error_code, "Error"))
        severity = "E"
        segments.append("|".join(["ERR", "", "", f"{error_code}^{description}^HL70357", severity, "", "", "",
                                  enc.escape_text(text)]))
    return "\r".join(segments) + "\r"
