"""Periodi ISTAT ("2024", "2024-Q3", "2024-08"): età del dato e frasi in italiano corretto.

Serve a scrivere "nel 2024", "nel 3° trimestre 2024", "ad agosto 2024" senza errori
di grammatica, e a decidere se un dato è abbastanza fresco da pubblicare.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]
FREQUENZE = {"A": "annuale", "Q": "trimestrale", "M": "mensile"}


@dataclass(frozen=True)
class Periodo:
    codice: str  # come arriva da ISTAT
    freq: str    # "A", "Q" o "M"
    anno: int
    num: int     # trimestre (1-4) o mese (1-12); 0 per gli annuali

    @classmethod
    def da_codice(cls, codice: str) -> "Periodo":
        codice = str(codice).strip()
        if m := re.fullmatch(r"(\d{4})", codice):
            return cls(codice, "A", int(m[1]), 0)
        if m := re.fullmatch(r"(\d{4})-Q([1-4])", codice):
            return cls(codice, "Q", int(m[1]), int(m[2]))
        if m := re.fullmatch(r"(\d{4})-(\d{2})", codice):
            return cls(codice, "M", int(m[1]), int(m[2]))
        raise ValueError(f"Periodo non riconosciuto: {codice!r}")

    @property
    def frequenza(self) -> str:
        return FREQUENZE[self.freq]

    def fine(self) -> date:
        """Ultimo giorno coperto dal periodo."""
        mese = 12 if self.freq == "A" else (self.num * 3 if self.freq == "Q" else self.num)
        return date(self.anno, mese, calendar.monthrange(self.anno, mese)[1])

    def eta_mesi(self, oggi: date | None = None) -> int:
        """Mesi trascorsi dalla fine del periodo (0 = periodo in corso o appena chiuso)."""
        oggi, fine = oggi or date.today(), self.fine()
        return max(0, (oggi.year - fine.year) * 12 + oggi.month - fine.month)

    def anno_prima(self) -> str:
        """Codice dello stesso periodo dell'anno precedente."""
        return self.codice.replace(str(self.anno), str(self.anno - 1), 1)

    # --- Frasi -------------------------------------------------------------------------
    @property
    def _mese(self) -> str:
        return MESI[self.num - 1]

    def _a(self) -> str:  # "a marzo" / "ad agosto"
        return "ad" if self._mese[0] in "aeiou" else "a"

    @property
    def nome(self) -> str:
        """2024 · 3° trimestre 2024 · agosto 2024"""
        if self.freq == "A":
            return str(self.anno)
        if self.freq == "Q":
            return f"{self.num}° trimestre {self.anno}"
        return f"{self._mese} {self.anno}"

    @property
    def breve(self) -> str:
        """Etichetta sotto le barre: 2024 · T3 24 · ago 24"""
        if self.freq == "A":
            return str(self.anno)
        if self.freq == "Q":
            return f"T{self.num} {self.anno % 100:02d}"
        return f"{self._mese[:3]} {self.anno % 100:02d}"

    @property
    def quando(self) -> str:
        """nel 2024 · nel 3° trimestre 2024 · ad agosto 2024"""
        return f"{self._a()} {self.nome}" if self.freq == "M" else f"nel {self.nome}"

    @property
    def da(self) -> str:
        """dal 2024 · dal 3° trimestre 2024 · da agosto 2024"""
        return f"da {self.nome}" if self.freq == "M" else f"dal {self.nome}"

    @property
    def al(self) -> str:
        """al 2024 · al 3° trimestre 2024 · ad agosto 2024"""
        return f"{self._a()} {self.nome}" if self.freq == "M" else f"al {self.nome}"

    @property
    def rispetto(self) -> str:
        """rispetto al 2024 · rispetto al 3° trimestre 2024 · rispetto ad agosto 2024"""
        return f"rispetto {self.al}"
