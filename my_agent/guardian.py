"""PII guardrail: detect and mask personal and financial identifiers BEFORE any model sees them, and again on the way out.
Checksum-validated where a standard exists (Luhn for cards, Verhoeff for Aadhaar) so ordinary numbers are not over-masked."""
import re

_D = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],[4,0,1,2,3,9,5,6,7,8],
      [5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],[8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]]
_P = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
      [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]]


def verhoeff_ok(num: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(num: str) -> str:
    c = 0
    for i, ch in enumerate(reversed(num)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str([0, 4, 3, 2, 1, 5, 6, 7, 8, 9][c])


def luhn_ok(num: str) -> bool:
    digits = [int(c) for c in num if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    s = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        s += d
    return s % 10 == 0


def _last(s, n=4):
    return "".join(c for c in s if c.isdigit())[-n:]


# (type, regex, validator or None, label builder). Most specific first so a card is not mistaken for a phone number.
_RULES = [
    ("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), lambda m, t: luhn_ok(m), lambda m: f"[CARD ••••{_last(m)}]"),
    ("AADHAAR", re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"),
     lambda m, t: verhoeff_ok("".join(c for c in m if c.isdigit())) or re.search(r"aadh?aa?r|uid", t[max(0, t.find(m) - 30):t.find(m)], re.I) is not None,
     lambda m: f"[AADHAAR ••••{_last(m)}]"),
    ("PAN", re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b"), lambda m, t: m[3].upper() in "PCHFATBLJG", lambda m: "[PAN]"),
    ("IFSC", re.compile(r"\b[A-Za-z]{4}0[A-Za-z0-9]{6}\b"), None, lambda m: "[IFSC]"),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), None, lambda m: "[EMAIL]"),
    ("UPI", re.compile(r"\b[\w.\-]{2,}@[a-z][a-z0-9]{2,}\b"), None, lambda m: "[UPI]"),
    ("ACCOUNT", re.compile(r"(?i)(?:account|a/c|acc)(?:\s*(?:no|number|num|#))?\.?\s*[:\-]?\s*(\d{9,18})"), None, None),
    ("PHONE", re.compile(r"(?<!\d)(?:\+91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), None, lambda m: f"[PHONE ••••{_last(m)}]"),
]


def mask(text: str):
    """Return {'masked': str, 'findings': [{'type','preview'}]}. Pure function: no I/O, no logging of raw values."""
    text = str(text or "")
    findings = []
    for kind, rx, valid, label in _RULES:
        if kind == "ACCOUNT":
            def acct(m):
                num = m.group(1)
                findings.append({"type": "ACCOUNT", "preview": f"••••{num[-4:]}"})
                return m.group(0).replace(num, f"[ACCOUNT ••••{num[-4:]}]")
            text = rx.sub(acct, text)
            continue

        def repl(m, kind=kind, valid=valid, label=label):
            raw = m.group(0)
            if valid is not None and not valid(raw, text):
                return raw
            out = label(raw)
            findings.append({"type": kind, "preview": out})
            return out + (" " if raw.endswith((" ", "-")) else "")
        text = rx.sub(repl, text)
    return {"masked": text, "findings": findings}
