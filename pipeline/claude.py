"""Chiamate a Claude Code in modalità non interattiva (`claude -p`), con il tuo abbonamento.

I testi dei prompt stanno in prompt/*.md: si possono migliorare senza toccare il codice.
I segnaposto nei prompt si scrivono come $nome.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from string import Template

from config import ROOT

PROMPT = ROOT / "prompt"
ISTRUZIONE = "Segui le istruzioni ricevute in input e rispondi solo con il JSON richiesto."
CONSUMI: list[dict] = []  # una voce per chiamata: per sapere quanto costa davvero una giornata


def riepilogo_consumi() -> str:
    """'7 chiamate · 41.230 token in entrata, 3.880 in uscita' (i token solo se Claude Code li riporta)."""
    if not CONSUMI:
        return "nessuna chiamata a Claude"
    testo = f"{len(CONSUMI)} chiamate a Claude"
    entrata = sum(c.get("input_tokens") or 0 for c in CONSUMI)
    uscita = sum(c.get("output_tokens") or 0 for c in CONSUMI)
    if entrata or uscita:
        punti = lambda n: f"{n:,}".replace(",", ".")  # noqa: E731  (migliaia all'italiana)
        testo += f" · {punti(entrata)} token in entrata, {punti(uscita)} in uscita"
    secondi = sum(c.get("secondi") or 0 for c in CONSUMI)
    return testo + f" · {secondi:.0f} secondi"


def prompt(nome: str, **valori) -> str:
    """Carica prompt/<nome>.md e sostituisce i segnaposto $nome."""
    return Template((PROMPT / f"{nome}.md").read_text(encoding="utf-8")).substitute(**valori)


def modello() -> str | None:
    """Il modello scelto in calendario.toml (modello_claude), es. "haiku": leggero e poco costoso."""
    import tomllib
    try:
        with open(ROOT / "calendario.toml", "rb") as fh:
            return tomllib.load(fh).get("modello_claude") or None
    except (OSError, ValueError):
        return None


def chiedi(testo: str, timeout: int = 240) -> dict:
    """Manda il prompt a Claude (con il modello del calendario) e restituisce l'oggetto JSON della risposta."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code non trovato nel PATH")
    inizio_t = time.monotonic()
    with tempfile.TemporaryDirectory() as cartella:  # cartella vuota: Claude non tocca il progetto
        comando = [exe, "-p", ISTRUZIONE, "--output-format", "json"]
        if m := modello():
            comando += ["--model", m]
        proc = subprocess.run(comando, input=testo,
                              capture_output=True, text=True, timeout=timeout, cwd=cartella)
    voce = {"secondi": time.monotonic() - inizio_t, "caratteri_prompt": len(testo)}
    CONSUMI.append(voce)
    try:
        risposta = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p ({proc.returncode}): {(proc.stderr or proc.stdout)[:400]}") from None
    uso = risposta.get("usage") or {}
    voce.update(input_tokens=(uso.get("input_tokens") or 0) + (uso.get("cache_read_input_tokens") or 0)
                + (uso.get("cache_creation_input_tokens") or 0), output_tokens=uso.get("output_tokens"))
    if proc.returncode != 0 or risposta.get("is_error"):
        raise RuntimeError(f"Errore da Claude: {str(risposta.get('result'))[:400]}")
    testo = risposta.get("result", "")
    inizio, fine = testo.find("{"), testo.rfind("}")
    if inizio < 0:
        raise RuntimeError(f"Nessun JSON nella risposta: {testo[:200]}")
    return json.loads(testo[inizio : fine + 1])
