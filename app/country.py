import unicodedata

_ISO_BY_NAME = {
    "ar": "AR",
    "argentina": "AR",
    "mx": "MX",
    "mexico": "MX",
    "cr": "CR",
    "costa rica": "CR",
}

_FULL_NAME_BY_ISO = {
    "AR": "Argentina",
    "MX": "Mexico",
    "CR": "Costa Rica",
}


def to_iso(*candidates: str | None, default: str = "AR") -> str:
    """
    Resuelve el código ISO del país a partir de valores heterogéneos.

    La tabla holidays usa códigos ISO ('AR', 'MX', 'CR') mientras que
    employees.country y employees.location mezclan códigos y nombres
    completos, así que hay que normalizar antes de buscar feriados.
    Devuelve el primer candidato que se pueda resolver.
    """
    for value in candidates:
        if not value:
            continue
        key = unicodedata.normalize("NFKD", value.strip().lower())
        key = "".join(c for c in key if not unicodedata.combining(c))
        iso = _ISO_BY_NAME.get(key)
        if iso:
            return iso
    return default


def to_calendar_name(*candidates: str | None, default: str = "Argentina") -> str:
    """Convierte cualquier formato de país al nombre usado en la tabla calendar."""
    iso = to_iso(*candidates, default=default)
    return _FULL_NAME_BY_ISO.get(iso, default)
