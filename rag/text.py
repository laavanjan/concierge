"""Tokenisation shared by BM25, the hashing embedder and the reranker."""

from __future__ import annotations

import re

STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "of", "to", "in", "on", "at", "by", "for", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "i", "me", "my", "we", "our", "you", "your", "it", "its", "this", "that", "these", "those", "there", "here", "what", "which", "who", "whom", "when", "where", "why", "how", "can", "could", "would", "should", "will", "shall", "may", "might", "must", "have", "has", "had", "not", "no", "yes", "so", "than", "too", "very", "just", "also", "about", "into", "over", "under", "after", "before", "up", "down", "out", "off", "any", "all", "some", "such", "only", "own", "same", "other", "more", "most", "please", "thanks", "thank", "hi", "hello", "hey", "get", "got", "want", "wanted", "like", "would", "i'd", "i'm", "im", "let", "lets", "tell", "know", "much", "many", "why", "hotel", "work", "works", "working", "need", "needs", "needing"]
)

PHRASES = [
    (re.compile(r"\bcheck[\s-]?in\b"), " checkin "),
    (re.compile(r"\bcheck[\s-]?out\b"), " checkout "),
    (re.compile(r"\bwi[\s-]?fi\b"), " wifi "),
    (re.compile(r"\be-?mail\b"), " email "),
    (re.compile(r"\bno[\s-]?show\b"), " noshow "),
    (re.compile(r"\bmoney back\b"), " refund "),
    (re.compile(r"\bcar park(ing)?\b"), " parking "),
    (re.compile(r"\bsea[\s-]?view\b"), " seaview "),
    (re.compile(r"\blate (cancel\w*)\b"), r" late \1 "),
    (re.compile(r"\b(the )?day before\b|\blast[\s-]minute\b|\bsame[\s-]day\b|\bwithin 24 hours\b"), " late "),
    (re.compile(r"\bleft (my|our|a|the|something)\b|\bleft behind\b"), " lost "),
    (re.compile(r"\bweek[\s-]?long\b|\b(a|one) week\b|\bweekly\b|\b7[\s-]days?\b"), " 7 night "),
    (re.compile(r"\b(guide|assistance|service|support) (dog|animal)s?\b"), " serviceanimal "),
]

SYNONYMS = {
    "dog": "pet", "dogs": "pet", "pets": "pet", "cat": "pet", "cats": "pet", "animal": "pet", "animals": "pet",
    "puppy": "pet",
    "kid": "child", "kids": "child", "children": "child", "baby": "child", "babies": "child", "toddler": "child",
    "infant": "child", "son": "child", "daughter": "child",
    "cancel": "cancellation", "cancelling": "cancellation", "canceling": "cancellation", "cancelled": "cancellation",
    "canceled": "cancellation", "cancellations": "cancellation", "cancels": "cancellation",
    "refunds": "refund", "refunded": "refund", "reimburse": "refund", "reimbursement": "refund",
    "change": "modify", "changes": "modify", "changing": "modify", "modification": "modify", "modifications": "modify",
    "reschedule": "modify", "amend": "modify", "move": "modify",
    "price": "rate", "prices": "rate", "rates": "rate", "cost": "rate", "costs": "rate", "pricing": "rate",
    "expensive": "rate", "cheaper": "rate", "charge": "fee", "charges": "fee", "fees": "fee",
    "arrive": "arrival", "arriving": "arrival", "arrival": "arrival", "depart": "departure", "leave": "departure",
    "leaving": "departure",
    "smoke": "smoking", "smoker": "smoking", "cigarette": "smoking", "vape": "smoking",
    "wheelchair": "accessible", "disabled": "accessible", "disability": "accessible", "accessibility": "accessible",
    "pool": "pool", "swimming": "pool", "swim": "pool", "gym": "gym", "fitness": "gym",
    "car": "parking", "park": "parking", "ev": "parking",
    "internet": "wifi", "wireless": "wifi",
    "airport": "airport", "shuttle": "transfer", "taxi": "transfer", "pickup": "transfer",
    "food": "breakfast", "meal": "breakfast", "dinner": "restaurant", "eat": "restaurant",
    "lost": "lost", "forgot": "lost", "forgotten": "lost",
    "group": "group", "groups": "group", "rooms": "room",
    "promo": "promotion", "promotion": "promotion", "coupon": "promotion", "code": "promotion", "discount": "discount",
    "discounts": "discount", "deal": "discount",
    "deposit": "deposit", "hold": "deposit", "pre-authorise": "deposit", "preauthorization": "deposit",
    "luggage": "luggage", "bags": "luggage", "baggage": "luggage", "suitcase": "luggage",
    "card": "card", "credit": "card", "cvv": "card",
    "weekends": "weekend", "friday": "weekend", "saturday": "weekend",
    "christmas": "festive", "xmas": "festive", "holiday": "festive", "holidays": "festive", "new year": "festive",
    "eve": "festive", "peak": "peak",
    "nights": "night", "stays": "stay", "staying": "stay", "longer": "long",
    "visit": "visitor", "visits": "visitor", "visiting": "visitor", "visitors": "visitor", "friend": "visitor",
    "friends": "visitor", "serviceanimal": "service",
    "charger": "item", "phone": "item", "wallet": "item", "keys": "item", "glasses": "item", "belongings": "item",
    "jacket": "item", "laptop": "item", "items": "item",
    "busy": "demand",
    "prepaid": "pay", "prepay": "pay", "paid": "pay", "paying": "pay", "booking": "book", "bookings": "book",
    "booked": "book", "popular": "demand", "occupancy": "demand",
}


def _stem(word: str) -> str:
    if len(word) <= 4:
        return word
    if word.endswith(("xes", "ches", "shes", "sses", "zes")):
        return word[:-2]
    for suffix, repl in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + repl
    return word


def normalize_text(text: str) -> str:
    lowered = text.lower()
    for pattern, repl in PHRASES:
        lowered = pattern.sub(repl, lowered)
    return lowered


def tokenize(text: str) -> list[str]:
    tokens = []
    for word in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", normalize_text(text)):
        if word in STOPWORDS:
            continue
        mapped = SYNONYMS.get(word)
        if mapped is None:
            stem = _stem(word)
            mapped = SYNONYMS.get(stem, stem)
        tokens.append(mapped)
    return tokens


def bigrams(tokens: list[str]) -> list[str]:
    return [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
