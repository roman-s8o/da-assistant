"""The last safety net: find text that looks like personal data and mask it.

The safe views keep personal data inside BigQuery, so these checks should never find
anything. They run on every query result before the model sees it, and on every final
answer before the user sees it. Each hit is counted and shown in the turn summary:
a hit means that another layer failed.

Measured on the dataset (2026-10-30): all 100,000 emails and all 100,000 street
addresses are caught, both as result values and inside answer text. False positives:
0 of 67,251 distinct text values (product names, brands, categories, SKUs, cities,
states, countries), and 1 of 27,309 product names inside answer text
("Michael Kors Men's 3 Pack Crew Neck").
"""

import re

import pandas as pd

from da_assistant.schema import DIRECT_PII

MASK = "[hidden]"

# Street name endings of US addresses. The dataset uses them for every country.
# A plain list of words reads better as one string than as one word per line.
_STREET_ENDINGS = (  # noqa: SIM905
    "Alley Avenue Ave Branch Bridge Brook Brooks Burg Burgs Bypass Camp Canyon Cape "
    "Causeway Center Centers Circle Circles Cliff Cliffs Club Common Corner Corners "
    "Course Court Courts Ct Cove Coves Creek Crescent Crest Crossing Crossroad Crossroads "
    "Curve Dale Dam Divide Drive Drives Dr Estate Estates Expressway Extension Extensions "
    "Fall Falls Ferry Field Fields Flat Flats Ford Fords Forest Forge Forges Fork Forks "
    "Fort Freeway Garden Gardens Gateway Glen Glens Green Greens Grove Groves Harbor "
    "Harbors Haven Heights Highway Hill Hills Hollow Inlet Island Islands Isle Junction "
    "Junctions Key Keys Knoll Knolls Lake Lakes Land Landing Lane Ln Light Lights Loaf "
    "Lock Locks Lodge Loop Mall Manor Manors Meadow Meadows Mews Mill Mills Mission "
    "Motorway Mount Mountain Mountains Neck Orchard Oval Overpass Park Parks Parkway "
    "Parkways Pass Passage Path Pike Pine Pines Place Plain Plains Plaza Point Points "
    "Port Ports Prairie Radial Ramp Ranch Rapid Rapids Rest Ridge Ridges River Road Roads "
    "Rd Route Row Rue Run Shoal Shoals Shore Shores Skyway Spring Springs Spur Spurs "
    "Square Squares Station Stravenue Stream Street Streets St Summit Terrace Throughway "
    "Trace Track Trafficway Trail Tunnel Turnpike Underpass Union Unions Valley Valleys "
    "Via Viaduct View Views Village Villages Ville Vista Walk Walks Wall Way Ways Well "
    "Wells"
).split()

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# +1 555 123 4567, (555) 123-4567, 555-123-4567.
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\w)"
)
# 123 Main Street, 6040 Webb Mountains Apt. 548.
_ADDRESS = (
    r"\d{1,6}\s+(?:[A-Z][a-z]+\s+){1,3}(?:"
    + "|".join(sorted(_STREET_ENDINGS, key=len, reverse=True))
    + r")\b(?:\s+(?:Apt\.|Suite)\s+\d+)?"
)
_ADDRESS_VALUE = re.compile(r"\s*" + _ADDRESS + r"\s*")
# In free text, an address ends the text, a sentence, a clause or a table cell. Product
# names that contain an address-like part ("2 Button Flat Front") go on with more words.
_ADDRESS_IN_TEXT = re.compile(r"\b" + _ADDRESS + r"(?=\s*(?:$|[,.;:)\n|]))")


def mask_text(text: str) -> tuple[str, int]:
    """Mask personal data in free text. Returns the text and the number of masked values."""
    hits = 0
    for pattern in (_EMAIL, _PHONE, _ADDRESS_IN_TEXT):
        text, count = pattern.subn(MASK, text)
        hits += count
    return text, hits


def mask_value(value: str) -> tuple[str, int]:
    """Mask personal data in one value of a query result."""
    if _ADDRESS_VALUE.fullmatch(value):
        return MASK, 1
    hits = 0
    for pattern in (_EMAIL, _PHONE):
        value, count = pattern.subn(MASK, value)
        hits += count
    return value, hits


def mask_rows(rows: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop personal data columns and mask personal data in text values."""
    hits = 0
    pii_columns = [name for name in rows.columns if str(name).lower() in DIRECT_PII]
    if pii_columns:
        hits += int(rows[pii_columns].notna().sum().sum())
        rows = rows.drop(columns=pii_columns)
    # Text columns: `str` in pandas 3, `object` in older versions or for mixed values.
    text_columns = [
        name
        for name in rows.columns
        if rows[name].dtype == object or pd.api.types.is_string_dtype(rows[name].dtype)
    ]
    if not text_columns:
        return rows, hits
    rows = rows.copy()
    for name in text_columns:
        masked = []
        for value in rows[name]:
            if isinstance(value, str):
                value, count = mask_value(value)
                hits += count
            masked.append(value)
        rows[name] = masked
    return rows, hits
