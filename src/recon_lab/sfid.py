"""Salesforce-shaped record IDs (15-char base62 body + 3-char case-safe suffix)."""

from __future__ import annotations

import random

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SUFFIX_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


class SalesforceId:
    """Build and check 18-character case-safe IDs.

    The suffix encodes, for each 5-character block of the 15-character ID, which
    positions are upper-case letters (first character = least-significant bit).
    """

    @staticmethod
    def suffix(id15: str) -> str:
        if len(id15) != 15:
            raise ValueError("a 15-character id is required")
        out = []
        for block in range(3):
            bits = 0
            for position in range(5):
                char = id15[block * 5 + position]
                if "A" <= char <= "Z":
                    bits |= 1 << position
            out.append(SUFFIX_ALPHABET[bits])
        return "".join(out)

    @staticmethod
    def to18(id15: str) -> str:
        return id15 + SalesforceId.suffix(id15)

    @staticmethod
    def is_valid18(value: str) -> bool:
        if not isinstance(value, str) or len(value) != 18:
            return False
        if any(char not in BASE62 for char in value[:15]):
            return False
        return SalesforceId.suffix(value[:15]) == value[15:]

    @staticmethod
    def make(key_prefix: str, pod: str, rng: random.Random) -> str:
        """key_prefix: 3 chars (e.g. '001' Account, '005' User); pod: 2 chars."""

        if len(key_prefix) != 3 or len(pod) != 2:
            raise ValueError("key_prefix must be 3 chars and pod 2 chars")
        body = "".join(rng.choice(BASE62) for _ in range(9))
        return SalesforceId.to18(f"{key_prefix}{pod}0{body}")
