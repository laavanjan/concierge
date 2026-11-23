"""Deterministic NLU: dates, party size, room type, references and intent rules.

Used three ways: as the zero-cost "rules" router, as a precise slot extractor
that runs before any model (regexes beat LLMs at "12-15 Dec" relative to a
fixed today), and as the brain of the offline/degraded agents.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from agents.schemas import BookingAction, Intent, RouterDecision, Slots
from common.domain import domain
from guardrails.pii import EMAIL_RE, find_phones

MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for m in names
}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
ORD = r"(?:st|nd|rd|th)?"
NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "couple": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14,
}
NUM = r"(\d{1,2}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen)"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _num(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else NUM_WORDS.get(token, 0)


def _future(day: int, month: int, year: int | None, today: date) -> date | None:
    try:
        candidate = date(year or today.year, month, day)
    except ValueError:
        return None
    if year is None and candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


# ----------------------------------------------------------------------------------------- dates
def extract_dates(text: str, today: date) -> tuple[date | None, date | None, int | None]:
    t = text.lower()
    check_in = check_out = None
    nights = None

    # ranges: "12-15 dec", "12 to 15 december", "dec 12-15", "between 12 and 15 dec"
    m = re.search(rf"\b(\d{{1,2}}){ORD}\s*(?:-|–|to|until|till|and)\s*(\d{{1,2}}){ORD}\s+(?:of\s+)?{MONTH_RE}(?:,?\s+(20\d\d))?", t)
    if m:
        month = MONTHS[_month_key(m.group(3))]
        year = int(m.group(4)) if m.group(4) else None
        start = _future(int(m.group(1)), month, year, today)
        end_day = int(m.group(2))
        if start:
            end_month, end_year = (start.month, start.year)
            if end_day <= start.day:
                end_month, end_year = (1, start.year + 1) if start.month == 12 else (start.month + 1, start.year)
            try:
                return start, date(end_year, end_month, end_day), None
            except ValueError:
                pass
    m = re.search(rf"\b{MONTH_RE}\s+(\d{{1,2}}){ORD}\s*(?:-|–|to|until|till)\s*(\d{{1,2}}){ORD}\b(?:,?\s+(20\d\d))?", t)
    if m:
        month = MONTHS[_month_key(m.group(1))]
        year = int(m.group(4)) if m.group(4) else None
        start = _future(int(m.group(2)), month, year, today)
        if start and int(m.group(3)) > start.day:
            return start, date(start.year, start.month, int(m.group(3))), None

    found: list[tuple[int, date, str]] = []
    for m in re.finditer(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b", t):
        try:
            found.append((m.start(), date(int(m.group(1)), int(m.group(2)), int(m.group(3))), t[max(0, m.start() - 25) : m.start()]))
        except ValueError:
            continue
    for m in re.finditer(rf"\b(\d{{1,2}}){ORD}\s+(?:of\s+)?{MONTH_RE}(?:,?\s+(20\d\d))?\b", t):
        d = _future(int(m.group(1)), MONTHS[_month_key(m.group(2))], int(m.group(3)) if m.group(3) else None, today)
        if d:
            found.append((m.start(), d, t[max(0, m.start() - 25) : m.start()]))
    for m in re.finditer(rf"\b{MONTH_RE}\s+(\d{{1,2}}){ORD}(?:,?\s+(20\d\d))?\b", t):
        d = _future(int(m.group(2)), MONTHS[_month_key(m.group(1))], int(m.group(3)) if m.group(3) else None, today)
        if d and all(abs(p - m.start()) > 3 for p, _, _ in found):
            found.append((m.start(), d, t[max(0, m.start() - 25) : m.start()]))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d\d|\d\d))?\b", t):
        a, b = int(m.group(1)), int(m.group(2))
        day, month = (a, b) if b <= 12 else (b, a)  # dd/mm by default, mm/dd when unambiguous
        year = int(m.group(3)) if m.group(3) else None
        if year and year < 100:
            year += 2000
        d = _future(day, month, year, today)
        if d:
            found.append((m.start(), d, t[max(0, m.start() - 25) : m.start()]))

    # relative dates
    rel = [
        (r"\bday after tomorrow\b", today + timedelta(days=2)),
        (r"\btomorrow\b", today + timedelta(days=1)),
        (r"\b(today|tonight)\b", today),
    ]
    for pattern, d in rel:
        m = re.search(pattern, t)
        if m:
            found.append((m.start(), d, t[max(0, m.start() - 25) : m.start()]))
            break
    m = re.search(r"\b(this|next)\s+weekend\b", t)
    if m:
        friday = today + timedelta(days=(4 - today.weekday()) % 7)
        if m.group(1) == "next" and (friday - today).days < 7:
            friday += timedelta(days=7)
        found.append((m.start(), friday, ""))
        nights = nights or 2
    for i, name in enumerate(WEEKDAYS):
        m = re.search(rf"\b(next|this|on|from|this coming)\s+{name}\b", t)
        if m:
            delta = (i - today.weekday()) % 7 or 7
            if m.group(1) == "next" and delta < 7:
                delta += 7 if delta <= 2 else 0
            found.append((m.start(), today + timedelta(days=delta), t[max(0, m.start() - 25) : m.start()]))

    found.sort(key=lambda x: x[0])
    dedup: list[tuple[date, str]] = []
    for _pos, d, before in found:
        if d not in [x[0] for x in dedup]:
            dedup.append((d, before))
    if len(dedup) >= 2:
        check_in, check_out = sorted([dedup[0][0], dedup[1][0]])
    elif len(dedup) == 1:
        d, before = dedup[0]
        if re.search(r"(check(ing)?[\s-]?out|leav\w*|depart\w*|until|till|through)\W*(on\W+)?$", before.strip()):
            check_out = d
        else:
            check_in = d

    m = re.search(rf"\b{NUM}[\s-]*nights?\b", t)
    if m:
        nights = _num(m.group(1))
    elif re.search(r"\b(a|one) week\b", t):
        nights = 7
    elif re.search(r"\b(two|2) weeks\b", t):
        nights = 14
    elif re.search(r"\bweekend\b", t) and nights is None and check_in and not check_out:
        nights = 2
    if check_in and check_out and check_out <= check_in:
        check_out = None
    return check_in, check_out, nights


def _month_key(token: str) -> str:
    token = token.rstrip(".").lower()
    if token.startswith("sept"):
        return "sept"
    for key in MONTHS:
        if token == key:
            return key
    return token[:3]


# ----------------------------------------------------------------------------------------- party
def extract_party(text: str) -> tuple[int | None, int | None]:
    t = text.lower()
    guests = None
    adults = re.search(rf"\b{NUM}\s+(?:adults?|grown[- ]?ups?)\b", t)
    kids = re.search(rf"\b{NUM}\s+(?:kids?|children|child|toddlers?|babies|baby|infants?|teens?|teenagers?)\b", t)
    if adults or kids:
        guests = (_num(adults.group(1)) if adults else 0) + (_num(kids.group(1)) if kids else 0)
        if not adults and kids:
            guests += 2 if re.search(r"\b(we|us|our|wife|husband|partner)\b", t) else 1
    if guests is None:
        m = re.search(rf"\b{NUM}\s+(?:people|persons?|guests?|pax|of us|travell?ers|visitors)\b", t)
        if m:
            guests = _num(m.group(1))
    if guests is None:
        m = re.search(r"\b(?:family|party|group) of (\d{1,2}|two|three|four|five|six|seven|eight)\b", t)
        if m:
            guests = _num(m.group(1))
    if guests is None:
        m = re.search(
            rf"\bfor\s+{NUM}\b(?!\s*(?:nights?|days?|weeks?|rooms?|months?|years?|hours?|pm|am|%|usd|dollars|\$|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|th|st|nd|rd|-|/))",
            t,
        )
        if m and m.group(1) not in ("a", "an"):
            guests = _num(m.group(1))
    if guests is None:
        if re.search(r"\b(me and my (wife|husband|partner|girlfriend|boyfriend|fiancee?|friend)|my (wife|husband|partner) and (i|me)|a couple|the two of us|both of us)\b", t):
            guests = 2
        elif re.search(r"\b(just me|only me|myself|solo|alone|by myself|single traveller)\b", t):
            guests = 1
    rooms = None
    m = re.search(rf"\b{NUM}\s+(?:\w+\s+)?(?:rooms?|suites?)\b", t)
    if m and m.group(1) not in ("a", "an"):
        rooms = _num(m.group(1))
    return (guests if guests and 0 < guests <= 40 else None), (rooms if rooms and 0 < rooms <= 20 else None)


# ----------------------------------------------------------------------------------------- ids & people
def extract_reservation_id(text: str) -> str | None:
    prefix = domain().property.reservation_prefix
    m = re.search(rf"\b{prefix}[-\s]?([A-Z0-9]{{6}})\b", text, re.I)
    return f"{prefix}-{m.group(1).upper()}" if m else None


def extract_email(text: str) -> str | None:
    m = EMAIL_RE.search(text)
    return m.group(0).lower() if m else None


NAME_STOP = {"looking", "interested", "trying", "here", "back", "not", "going", "planning", "traveling", "travelling",
             "staying", "calling", "writing", "the", "a", "an", "so", "very", "just", "also", "booking", "wondering"}


def extract_name(text: str) -> str | None:
    patterns = [
        (r"\b(?:my name is|name is|name:|names?\s*-)\s*([a-zA-Z][a-zA-Z'’-]+(?:\s+[a-zA-Z][a-zA-Z'’-]+){0,2})", re.I),
        (r"\b[Uu]nder (?:the name (?:of )?)?([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+){0,2})", 0),
        (r"\b(?:I am|I'm|[Tt]his is|[Ii]t's)\s+([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+){1,2})", 0),
    ]
    for pattern, flags in patterns:
        m = re.search(pattern, text, flags)
        if m:
            words = [w for w in m.group(1).split() if w.lower() not in NAME_STOP]
            if words and words[0].lower() not in NAME_STOP and len(words) >= 1:
                stop_at = next((i for i, w in enumerate(words) if w.lower() in ("and", "with", "email", "my", "at")), None)
                words = words[:stop_at] if stop_at else words
                if words:
                    return " ".join(w.capitalize() if w.islower() else w for w in words)
    email = EMAIL_RE.search(text)
    if email:
        before = text[: email.start()].strip(" ,;:-")
        m = re.search(r"([A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’-]+){1,2})$", before)
        if m:
            return m.group(1)
    return None


def extract_promo(text: str) -> str | None:
    codes = domain().rate_rules.promo_codes
    for code in codes:
        if re.search(rf"\b{re.escape(code)}\b", text, re.I):
            return code
    m = re.search(r"\b(?:promo(?:tion(?:al)?)?|discount|coupon|voucher)\s*code\s*(?:is\s*)?[:#]?\s*([A-Za-z0-9]{4,15})\b", text, re.I)
    return m.group(1).upper() if m else None


def extract_money(text: str, *keywords: str) -> float | None:
    t = text.lower()
    for kw in keywords:
        m = re.search(rf"{kw}[^$\d]{{0,25}}(?:\$|usd\s?)?\s?(\d+(?:[.,]\d{{1,2}})?)", t)
        if m:
            return float(m.group(1).replace(",", "."))
    m = re.search(r"\$\s?(\d+(?:\.\d{1,2})?)", t)
    return float(m.group(1)) if m and keywords else None


def extract_slots(text: str, today: date) -> Slots:
    check_in, check_out, nights = extract_dates(text, today)
    guests, rooms = extract_party(text)
    phones = find_phones(text)
    lowered = text.lower()
    return Slots(
        check_in=check_in,
        check_out=check_out,
        nights=nights,
        guests=guests,
        rooms=rooms,
        room_type=domain().resolve_room_alias(text),
        reservation_id=extract_reservation_id(text),
        guest_email=extract_email(text),
        guest_phone=phones[0] if phones else None,
        guest_name=extract_name(text),
        promo_code=extract_promo(text),
        refund_amount=extract_money(text, "refund", "back", "compensat") if "refund" in lowered or "compensat" in lowered else None,
        target_total=extract_money(text, "match", "for", "down to", "budget") if re.search(r"\b(match|override|discount it|do it for|down to|best price|lower the price)\b", lowered) else None,
    )


# ----------------------------------------------------------------------------------------- intents
YES = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|go ahead|book it|do it|please do|sounds good|that works|"
    r"perfect|great|absolutely|definitely|correct|let'?s do it|proceed|that'?s fine|fine|deal|yes please)\b",
    re.I,
)
NO = re.compile(r"^\s*(no|nope|nah|don'?t|do not|not now|never ?mind|cancel that|stop|wait|hold on)\b", re.I)
HANDOFF = re.compile(
    r"\b((speak|talk|chat)\s+(to|with)\s+(a |an |the )?(human|person|someone|agent|manager|representative|staff|people)|"
    r"real person|human agent|live agent|customer service|an operator|front desk staff)\b",
    re.I,
)
GREETING = re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|hiya|yo|greetings)\b[!. ]*$", re.I)
THANKS = re.compile(r"^\s*(thanks|thank you|cheers|great,? thanks|perfect,? thanks|ty)\b[!. ]*$", re.I)
POLICY_WORDS = re.compile(
    r"\b(polic(y|ies)|rules?|allowed|permit\w*|can i bring|pets?|dogs?|cats?|children|kids?|baby|cot|check[\s-]?in|"
    r"check[\s-]?out|breakfast|parking|park|wi-?fi|internet|pool|gym|smok\w*|accessib\w*|wheelchair|airport|transfer|"
    r"shuttle|luggage|deposit|refundable|refunds? take|payment methods?|pay(ment)? by|cards? do you|privacy|my data|"
    r"lost|forgot|left my|fire|quiet hours|restaurant|room service|dinner|visitors?|safe|damage|id required|age|"
    r"extra bed|what happens if|is there a fee|fees?|how long|what time|opening hours|open\b|terms|faq|groups?|tax(es)?|"
    r"pay|visit\w*|friends?|change the name|name change|longest|maximum)\b",
    re.I,
)
CANCEL_ACTION = re.compile(
    r"\b(cancel|cancell?ing)\b\s+(my|the|this|our|a|it|booking|reservation|stay|\w{3}-\w{6})|"
    r"\b(want|need|like|have) to cancel\b|\bplease cancel\b|\bcancel it\b|^\s*cancel\b",
    re.I,
)
MODIFY_ACTION = re.compile(
    r"\b(change|modify|move|reschedule|amend|extend|shorten|switch|upgrade|downgrade|push|shift)\b[^.?!]{0,40}"
    r"\b(booking|reservation|dates?|stay|check[\s-]?in|check[\s-]?out|to|from|room|it|night)\b",
    re.I,
)
LOOKUP_ACTION = re.compile(
    r"\b(look ?up|check on|find|see|status of|details of|show me|what'?s|confirm)\b[^.?!]{0,25}\b(my|the)\s+(booking|reservation)\b",
    re.I,
)
REFUND_ACTION = re.compile(
    r"\b(want|need|like|request|deserve|get|give me|asking for|issue)\b[^.?!]{0,20}\b(a |my |partial |full |some )?(refund|money back|compensation)\b",
    re.I,
)
BOOK_WORDS = re.compile(
    r"\b(book|reserve|reservation|availability|available|vacanc\w+|room for|a room|stay|staying|nights?|"
    r"check in on|arriv\w+|get a room|need a room|looking for a room)\b",
    re.I,
)
PRICE_WORDS = re.compile(
    r"\b(price|prices|pricing|cost|costs|how much|rate|rates|quote|cheap\w*|expensive|discount|promo\w*|deal|"
    r"total|budget|afford|per night|why .{0,20}(higher|more|pricier)|price match|match .{0,10}price)\b",
    re.I,
)
AMENITY_PRICE = re.compile(
    r"\b(parking|breakfast|transfer|shuttle|pet|dog|late check[\s-]?out|early check[\s-]?in|extra bed|deposit|"
    r"room service|spa|gym|pool)\b",
    re.I,
)
OUT_OF_SCOPE = re.compile(
    r"\b(weather|stock|crypto|bitcoin|poem|essay|homework|python|javascript|recipe|politic\w*|election|football|"
    r"capital of|joke|song|lyrics|translate)\b",
    re.I,
)


_QWORD = r"(?:what|how|can|could|do|does|is|are|when|where|which|why|will)"


def policy_clause(text: str) -> str:
    """The policy part of a compound message, used as the retrieval query.

    "Book 3 nights from 12 Dec, what's the cancellation policy?" retrieves better as "what's the cancellation
    policy?": the booking words otherwise pull in sections about stays and payment.
    """
    parts = [
        p.strip()
        for p in re.split(rf"(?<=[?.!])\s+|,\s*(?=(?:and |also |plus )?{_QWORD}\b)|\s+(?:and|also)\s+(?={_QWORD}\b)", text, flags=re.I)
        if p and p.strip()
    ]
    chosen = [p for p in parts if POLICY_WORDS.search(p)] or [p for p in parts if re.match(rf"^{_QWORD}\b", p, re.I)]
    return " ".join(chosen) if chosen and len(chosen) < len(parts) else text


def rule_decision(text: str, today: date, flow: dict | None = None, has_offer: bool = False) -> RouterDecision:
    """Zero-cost router: keyword rules + regex slot extraction."""
    flow = flow or {}
    awaiting = flow.get("awaiting")
    slots = extract_slots(text, today)
    intents: list[Intent] = []
    action: BookingAction = "none"
    confirm = True if YES.search(text) else (False if NO.search(text) else None)
    stripped = text.strip()

    if HANDOFF.search(text):
        return RouterDecision(intents=["handoff"], slots=slots, source="rules")
    if (GREETING.match(stripped) or THANKS.match(stripped)) and not awaiting:
        return RouterDecision(intents=["smalltalk"], slots=slots, source="rules")

    policy_hit = bool(POLICY_WORDS.search(text))
    stated = slots.model_dump(exclude_none=True)
    if (
        OUT_OF_SCOPE.search(text)
        and not {"guests", "reservation_id", "guest_email", "room_type"} & set(stated)
        and not policy_hit
        and not PRICE_WORDS.search(text)
        and not re.search(r"\b(book|reserve|room|stay|suite|hotel)\b", text, re.I)
    ):
        return RouterDecision(intents=["out_of_scope"], slots=slots, source="rules")
    # "Can I cancel for free?" / "Is tax included?" are questions about the rules, not requests to act:
    # without concrete details (dates, party, reference, email) they go to the Policy agent.
    concrete = bool(slots.check_in or slots.check_out or slots.guests or slots.rooms or slots.reservation_id or slots.guest_email)
    request = re.search(
        r"\b(can you|could you|would you|will you|please|i want|i'd like|i would like|i need|book me|reserve me|let'?s|go ahead)\b",
        text, re.I,
    )
    asks = stripped.endswith("?") or re.match(
        r"^\s*(can|could|do|does|is|are|what|when|how|why|which|where|will|would|should|if|am|may|tell me)\b", stripped, re.I
    )
    if asks and not concrete and not request and confirm is None:
        return RouterDecision(intents=["policy"], slots=slots, policy_question=text, source="rules")
    if CANCEL_ACTION.search(text) and not re.search(r"\b(polic(y|ies)|what happens|can i cancel|if i cancel|free cancel)", text, re.I):
        action = "cancel"
    elif REFUND_ACTION.search(text) and not re.search(r"\bhow long\b", text, re.I):
        action = "refund"
    elif MODIFY_ACTION.search(text) and (
        slots.reservation_id or slots.check_in or slots.check_out or slots.room_type or slots.guests or flow.get("action") == "modify"
        or re.search(r"\bmy (booking|reservation|stay)\b", text, re.I)
    ) and not (re.search(r"\b(can i|is it possible|policy)\b", text, re.I) and not (slots.reservation_id or slots.check_in)):
        action = "modify"
    elif LOOKUP_ACTION.search(text) or (slots.reservation_id and not flow.get("action")):
        action = "lookup"

    price_hit = bool(PRICE_WORDS.search(text))
    amenity_price = price_hit and bool(AMENITY_PRICE.search(text)) and not (slots.check_in or slots.room_type)
    stay_info = bool(slots.check_in or slots.check_out or slots.nights or slots.guests or slots.rooms)

    if action != "none":
        intents.append("booking")
    elif price_hit and not amenity_price and (stay_info or slots.room_type or has_offer or not policy_hit):
        if re.search(r"\b(book|reserve)\b", text, re.I) and stay_info:
            intents.append("booking")
            action = "new"
        else:
            intents.append("pricing")
    elif (BOOK_WORDS.search(text) or stay_info or slots.room_type) and not (
        policy_hit and not stay_info and not re.search(r"\b(book|reserve)\b", text, re.I)
    ):
        intents.append("booking")
        action = "new"

    asks_policy = re.search(r"\?|polic|what|how|can i|do you|is there|are there|allowed", text, re.I)
    if (policy_hit or amenity_price) and (not intents or asks_policy) and "policy" not in intents:
        intents.append("policy")

    if not intents:
        if awaiting:
            agent = flow.get("agent", "booking")
            intents.append(agent if agent in ("booking", "pricing") else "booking")
            action = flow.get("action", "new") if agent == "booking" else "none"
        elif confirm is not None and has_offer:
            intents.append("booking")
            action = "new"
        elif slots.guest_email or slots.guest_name:
            intents.append("booking")
            action = flow.get("action") or "new"
        elif OUT_OF_SCOPE.search(text):
            intents.append("out_of_scope")
        else:
            intents.append("policy")
    if "booking" in intents and action == "none":
        action = flow.get("action") or "new"
    # A bare "yes"/"no" continues whatever the assistant proposed last.
    if confirm is not None and awaiting and intents == ["policy"]:
        intents = [flow.get("agent", "booking")]
        action = flow.get("action", "new")
    policy_question = policy_clause(text) if "policy" in intents else None
    return RouterDecision(
        intents=intents, booking_action=action, confirm=confirm, policy_question=policy_question, slots=slots, source="rules"
    )
