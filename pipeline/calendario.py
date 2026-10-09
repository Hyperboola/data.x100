"""Lettura e controllo di calendario.toml e preferiti.toml."""
from __future__ import annotations

import tomllib
from pathlib import Path

from config import ROOT

FREQUENZE = ("annuale", "trimestrale", "mensile")
GIORNI = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
CAMPI_SERIE = ("id", "frequenza", "flow", "filtri", "occhiello", "domanda", "soggetto")


def carica_calendario(path: Path = ROOT / "calendario.toml") -> dict:
    with open(path, "rb") as fh:
        cal = tomllib.load(fh)
    if fasce := [s for s in cal["slot"] if s not in FREQUENZE]:
        raise ValueError(f"calendario.toml: fasce sconosciute {fasce} (ammesse: {FREQUENZE})")
    cal["sottotemi"], cal["giorno_di"] = {}, {}
    for giorno in GIORNI:
        for sott, codici in cal["giorni"].get(giorno, {}).get("sottotemi", {}).items():
            if sott in cal["sottotemi"]:
                raise ValueError(f"calendario.toml: sottotema {sott!r} in due giorni")
            cal["sottotemi"][sott], cal["giorno_di"][sott] = codici, giorno
    racconto = cal.setdefault("racconto", {})
    if sconosciuti := set(racconto) - set(cal["sottotemi"]) - {"default"}:
        raise ValueError(f"calendario.toml, [racconto]: sottotemi sconosciuti {sorted(sconosciuti)}")
    cal.setdefault("usa_claude", True)
    if not cal["usa_claude"]:
        cal["scelta_dataset"] = "recente"
    return cal


def preferenze(cal: dict, sottotema: str) -> dict:
    """Come raccontare un sottotema: la sua voce in [racconto], completata da quella di default."""
    return {**cal.get("racconto", {}).get("default", {}), **cal.get("racconto", {}).get(sottotema, {})}


def carica_preferiti(cal: dict, path: Path = ROOT / "preferiti.toml") -> list[dict]:
    """Serie scelte a mano, pubblicate nei giorni con preferiti = true (file facoltativo)."""
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        serie = tomllib.load(fh).get("serie", [])
    for s in serie:
        if mancanti := [c for c in CAMPI_SERIE if c not in s]:
            raise ValueError(f"preferiti.toml, {s.get('id', '?')}: mancano {mancanti}")
        if s["frequenza"] not in FREQUENZE:
            raise ValueError(f"preferiti.toml, {s['id']}: frequenza non valida")
        s.setdefault("sottotema", "preferiti")
    return serie
