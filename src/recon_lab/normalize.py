"""Deterministic normalisation used by blocking and the baseline model."""

from __future__ import annotations

import re

LEGAL_TOKENS = frozenset({"inc", "llc", "corp", "corporation", "co", "ltd", "limited", "company", "incorporated"})
STREET_WORDS = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "boulevard": "blvd",
    "lane": "ln", "court": "ct", "suite": "ste", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "square": "sq", "terrace": "ter", "apartment": "apt",
}
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


class Soundex:
    """American Soundex (H and W do not separate equal codes)."""

    CODES = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"),
             "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}

    @staticmethod
    def encode(word: str | None) -> str | None:
        letters = [c for c in (word or "").lower() if c.isalpha()]
        if not letters:
            return None
        first = letters[0]
        out = [first.upper()]
        previous = Soundex.CODES.get(first, "")
        for char in letters[1:]:
            code = Soundex.CODES.get(char, "")
            if code and code != previous:
                out.append(code)
            if char not in "hw":
                previous = code
        return ("".join(out) + "000")[:4]


class Normalizer:
    @staticmethod
    def _clean(value: str | None) -> str:
        text = (value or "").lower().replace("&", " and ")
        text = _NON_ALNUM.sub(" ", text)
        return _SPACES.sub(" ", text).strip()

    @staticmethod
    def name(value: str | None) -> str | None:
        tokens = [t for t in Normalizer._clean(value).split(" ") if t and t not in LEGAL_TOKENS]
        return " ".join(tokens) or None

    @staticmethod
    def domain(value: str | None) -> str | None:
        text = (value or "").strip().lower()
        if not text:
            return None
        text = re.sub(r"^[a-z]+://", "", text)
        host = text.split("/")[0].split("?")[0].split(":")[0].strip(".")
        if host.startswith("www."):
            host = host[4:]
        labels = [label for label in host.split(".") if label]
        if len(labels) < 2:
            return None
        return ".".join(labels[-2:])

    @staticmethod
    def phone(value: str | None) -> str | None:
        digits = re.sub(r"\D", "", value or "")
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits if len(digits) == 10 else None

    @staticmethod
    def postcode5(value: str | None) -> str | None:
        digits = re.sub(r"\D", "", value or "")
        return digits[:5] if len(digits) >= 5 else None

    @staticmethod
    def street(value: str | None) -> str | None:
        tokens = [STREET_WORDS.get(t, t) for t in Normalizer._clean(value).split(" ") if t]
        return " ".join(tokens) or None

    @staticmethod
    def simple(value: str | None) -> str | None:
        return Normalizer._clean(value) or None

    @staticmethod
    def state(value: str | None) -> str | None:
        text = (value or "").strip().upper()
        return text or None

    @staticmethod
    def name4(value: str | None) -> str | None:
        norm = Normalizer.name(value)
        return norm.replace(" ", "")[:4] if norm else None

    @staticmethod
    def soundex_first(value: str | None) -> str | None:
        norm = Normalizer.name(value)
        return Soundex.encode(norm.split(" ")[0]) if norm else None

    @staticmethod
    def record(row: dict[str, str], org: str) -> dict[str, object]:
        """Add normalised blocking/comparison columns to one exported Account row."""

        return {
            "unique_id": f"{org}:{row['Id']}",
            "source_dataset": org,
            "name_norm": Normalizer.name(row.get("Name")),
            "domain_norm": Normalizer.domain(row.get("Website")),
            "phone10": Normalizer.phone(row.get("Phone")),
            "postcode5": Normalizer.postcode5(row.get("BillingPostalCode")),
            "street_norm": Normalizer.street(row.get("BillingStreet")),
            "city_norm": Normalizer.simple(row.get("BillingCity")),
            "state_norm": Normalizer.state(row.get("BillingState")),
            "name4": Normalizer.name4(row.get("Name")),
            "soundex_first": Normalizer.soundex_first(row.get("Name")),
        }
