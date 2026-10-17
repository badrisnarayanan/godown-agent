"""Groundedness and truthfulness check: every factual claim in a reply must be traceable to tool results (evidence).
Deterministic, so it can run on every reply without an extra model call."""
import json
import re

PROMISE = re.compile(r"\b(guarantee[sd]?|promise[sd]?|free of charge|free shipping|free delivery|discount(?:ed)?|refund|cashback|"
                     r"tomorrow|tonight|same[- ]day|by \d{1,2}(?::\d{2})?\s?(?:am|pm)|within \d+\s*(?:minutes?|hours?|days?)|100%)", re.I)
SAFE_REPLY = ("I can only tell you what I can verify in our system, so I can't confirm that. I can place your order now, "
              "and the tracking page will show the live delivery estimate.")


def verify(text: str, evidence, user_text: str = ""):
    """Return {'claims': [...], 'total', 'verified', 'flagged'}. Numbers may come from the evidence or the user's own message;
    tracking IDs, SKUs and promises (discounts, delivery guarantees) must come from the evidence."""
    ev = (evidence if isinstance(evidence, str) else json.dumps(evidence, default=str)).lower()
    ev_digits = ev.replace(",", "")
    user = (user_text or "").lower().replace(",", "")
    claims = []
    for m in re.finditer(r"TRK[0-9A-F]{8}", text or ""):
        claims.append({"kind": "tracking id", "text": m.group(0), "ok": m.group(0).lower() in ev})
    for m in re.finditer(r"\b[A-Z][A-Z0-9]{1,}-[A-Z0-9]{1,}\b", text or ""):
        if not m.group(0).startswith("TRK"):
            kind = "order id" if m.group(0).startswith("ORD-") else "product"
            claims.append({"kind": kind, "text": m.group(0), "ok": m.group(0).lower() in ev})
    for m in re.finditer(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", text or ""):
        n = m.group(0).replace(",", "").rstrip(".")
        whole = re.compile(rf"(?<![\d.]){re.escape(n)}(?![\d])")  # whole numbers only: "3" must not match inside "13"
        claims.append({"kind": "number", "text": m.group(0), "ok": bool(whole.search(ev_digits) or whole.search(user))})
    for m in PROMISE.finditer(text or ""):
        claims.append({"kind": "promise", "text": m.group(0), "ok": m.group(0).lower() in ev})
    flagged = [c["text"] for c in claims if not c["ok"]]
    return {"claims": claims, "total": len(claims), "verified": len(claims) - len(flagged), "flagged": flagged}
