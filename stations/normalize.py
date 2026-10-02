"""Text normalization shared by the offline pipeline and runtime geocoding.

Pure functions, no Django imports.
"""

from __future__ import annotations

import re

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip

_STATE_BY_NAME: dict[str, str] = {name.casefold(): code for code, name in US_STATES.items()}
_STATE_BY_NAME["washington dc"] = "DC"
_STATE_BY_NAME["washington d.c."] = "DC"

# Contiguous USA bounding box used for input validation and geocode sanity checks.
CONUS_BBOX = {"min_lat": 24.0, "max_lat": 50.0, "min_lon": -125.0, "max_lon": -66.5}

_WS = re.compile(r"\s+")
_ABBREVIATIONS = [
    (re.compile(r"\bst\.?(?=\s)", re.IGNORECASE), "saint"),
    (re.compile(r"\bste\.?(?=\s)", re.IGNORECASE), "sainte"),
    (re.compile(r"\bft\.?(?=\s)", re.IGNORECASE), "fort"),
    (re.compile(r"\bmt\.?(?=\s)", re.IGNORECASE), "mount"),
    (re.compile(r"\bpt\.?(?=\s)", re.IGNORECASE), "point"),
]
_PUNCT = re.compile("[.'\u2019`\"]")  # period, ASCII/curly apostrophes, backtick, double quote
_DASHES = re.compile(r"[-/]")
# "Mc Lean" / "McLean", "La Fayette" / "LaFayette", "Du Bois" / "DuBois", "De Forest" / "DeForest":
# both spellings are common, so the key joins the prefix to the following word.
_JOINED_PREFIX = re.compile(r"\b(mc|la|le|de|du)\s+(?=[a-z])")
_TRAILING_QUALIFIER = re.compile(r"\s+national park$")


def repair_mojibake(text: str) -> str:
    """Undo UTF-8 text that was mis-decoded as Windows-1252 upstream.

    The source CSV contains ``"Stuckeyâ€™s"`` for ``"Stuckey's"`` (with a U+2019 apostrophe). If
    re-encoding as cp1252 and decoding as UTF-8 round-trips cleanly, that is what happened;
    otherwise the text is returned unchanged.
    """
    if text.isascii():
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def normalize_city(city: str) -> str:
    """Canonical lookup key for a city name.

    ``"  St. Louis "`` → ``"saint louis"``; ``"Winston-Salem"`` → ``"winston salem"``;
    ``"Mc Lean"`` → ``"mclean"``.
    """
    text = _WS.sub(" ", city.strip())
    text = _DASHES.sub(" ", text)
    for pattern, replacement in _ABBREVIATIONS:
        text = pattern.sub(replacement, text)
    text = _PUNCT.sub("", text)
    text = _WS.sub(" ", text).strip().casefold()
    text = _TRAILING_QUALIFIER.sub("", text)
    text = _JOINED_PREFIX.sub(r"\1", text)
    return text


def normalize_state(state: str) -> str | None:
    """Return the two-letter USPS code for a state code or full name, else None."""
    text = _WS.sub(" ", state.strip())
    if not text:
        return None
    upper = text.upper()
    if upper in US_STATES:
        return upper
    return _STATE_BY_NAME.get(text.casefold())


def normalize_address(address: str) -> str:
    """Loose key for spotting the same physical location listed under two IDs."""
    text = _WS.sub(" ", address.strip()).casefold()
    text = re.sub(r"[^a-z0-9&\s]", "", text)
    return _WS.sub(" ", text).strip()


def in_conus(lat: float, lon: float) -> bool:
    b = CONUS_BBOX
    return b["min_lat"] <= lat <= b["max_lat"] and b["min_lon"] <= lon <= b["max_lon"]
