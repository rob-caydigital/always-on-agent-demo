M_TO_FT = 3.28084
KMH_TO_KT = 0.539957
MS_TO_KT = 1.94384


def to_ft(value: float | None, uom: str | None) -> float | None:
    if value is None:
        return None
    if uom and uom.endswith(":m"):
        return value * M_TO_FT
    return value  # already feet


def to_kt(value: float | None, uom: str | None) -> float | None:
    if value is None:
        return None
    if uom == "wmoUnit:km_h-1":
        return value * KMH_TO_KT
    if uom == "wmoUnit:m_s-1":
        return value * MS_TO_KT
    return value  # already knots
