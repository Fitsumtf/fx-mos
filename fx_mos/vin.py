"""VIN / serial allocation.

Every unit that enters the line needs an identity before it has any physical
parts. This module implements real ISO 3779 check-digit math so that the serials
the MOS hands out survive validation by downstream systems (ERP, dealer DMS,
regulatory filings) instead of being decorative strings.
"""

from __future__ import annotations

import datetime as dt

# Position 9 is the check digit. Weights are fixed by the standard.
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

_TRANSLITERATION = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}

# I, O and Q are excluded from VINs to avoid confusion with 1 and 0.
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"

# Reserved for FitEx Industrial. Replace with your assigned WMI before shipping
# serials to a regulator.
DEFAULT_WMI = "FX1"


class VinError(ValueError):
    """Raised when a VIN is structurally invalid."""


def model_year_code(year: int) -> str:
    """Return the single-character model-year code for a calendar year."""
    if not 1980 <= year <= 2039:
        raise VinError(f"model year {year} outside the encodable range 1980-2039")
    return _YEAR_CODES[(year - 1980) % 30]


def check_digit(vin17: str) -> str:
    """Compute the position-9 check digit for a 17-character VIN."""
    if len(vin17) != 17:
        raise VinError(f"VIN must be 17 characters, got {len(vin17)}")
    total = 0
    for char, weight in zip(vin17.upper(), _WEIGHTS):
        if char not in _TRANSLITERATION:
            raise VinError(f"character {char!r} is not valid in a VIN")
        total += _TRANSLITERATION[char] * weight
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def is_valid(vin: str) -> bool:
    """True when the VIN is 17 characters and its check digit agrees."""
    try:
        return check_digit(vin) == vin.upper()[8]
    except (VinError, IndexError):
        return False


def allocate(
    *,
    sequence: int,
    model_code: str,
    plant_code: str,
    wmi: str = DEFAULT_WMI,
    year: int | None = None,
) -> str:
    """Allocate a VIN for one unit.

    ``sequence`` is the plant's monotonic build counter. The caller owns
    uniqueness; this function only guarantees the string is well formed.
    """
    if len(wmi) != 3:
        raise VinError("WMI must be exactly 3 characters")
    if len(plant_code) != 1:
        raise VinError("plant code must be exactly 1 character")

    year = year or dt.date.today().year
    descriptor = model_code.upper().ljust(5, "0")[:5]  # positions 4-8
    serial = f"{sequence:06d}"[-6:]  # positions 12-17

    body = f"{wmi.upper()}{descriptor}0{model_year_code(year)}{plant_code.upper()}{serial}"
    return body[:8] + check_digit(body) + body[9:]
