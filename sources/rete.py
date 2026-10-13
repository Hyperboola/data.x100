"""Richieste HTTP gentili verso servizi pubblici: pausa tra le richieste, pochi tentativi, errori chiari."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class ErroreFonte(RuntimeError):
    """La fonte ha risposto con un errore o non ha risposto."""


class FonteNonDisponibile(ErroreFonte):
    """Il servizio risponde ma il suo database è irraggiungibile: inutile insistere ora."""


class NessunDato(ErroreFonte):
    """La richiesta è corretta ma non ci sono dati (es. combinazione di codici inesistente)."""


class Client:
    def __init__(self, user_agent: str, pausa: float = 13.0, timeout: float = 180.0,
                 tentativi: int = 3, session: requests.Session | None = None):
        self.user_agent = user_agent
        self.pausa = pausa            # secondi minimi tra due richieste
        self.timeout = timeout
        self.tentativi = tentativi
        self.session = session or requests.Session()
        self._ultima = float("-inf")

    def richiesta(self, url: str, accept: str, params: dict | None = None, corpo: list | dict | None = None,
                  intestazioni: dict | None = None, timeout: float | None = None,
                  tentativi: int | None = None) -> requests.Response:
        """GET (o POST se c'è un corpo JSON). Riprova solo sugli errori del server."""
        headers = {"Accept": accept, "User-Agent": self.user_agent, **(intestazioni or {})}
        tentativi = tentativi or self.tentativi
        errore: Exception | None = None
        for n in range(1, tentativi + 1):
            attesa = self.pausa - (time.monotonic() - self._ultima)
            if attesa > 0:
                time.sleep(attesa)
            self._ultima = time.monotonic()
            # (connessione, lettura): se il server non si raggiunge lo sappiamo in 10 secondi, mentre per
            # scaricare un file grande si può aspettare a lungo
            limiti = (10, timeout or self.timeout)
            try:
                if corpo is None:
                    resp = self.session.get(url, headers=headers, params=params, timeout=limiti)
                else:
                    resp = self.session.post(url, headers=headers, json=corpo, timeout=limiti)
            except (requests.ConnectionError, requests.ConnectTimeout) as exc:
                if not isinstance(exc, requests.ReadTimeout):  # il server non si raggiunge: inutile insistere
                    raise FonteNonDisponibile(f"{url}: server non raggiungibile ({type(exc).__name__})") from exc
                errore = exc
            except requests.RequestException as exc:
                errore = exc
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 204 or (resp.status_code == 404 and ("NoRecordsFound" in resp.text
                                                                             or "/rest/data/" in url)):
                    raise NessunDato(f"nessun dato da {url}")  # inutile riprovare
                errore = ErroreFonte(f"HTTP {resp.status_code} da {url}: {resp.text[:300]}")
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise errore  # errore nostro (id o filtri sbagliati): inutile insistere
                if resp.status_code >= 500 and "SQL Server" in resp.text:
                    raise FonteNonDisponibile(f"{url}: il servizio non raggiunge il suo database")
            if n < tentativi:
                log.warning("%s: tentativo %s/%s fallito (%s)", url, n, tentativi, errore)
                time.sleep(self.pausa * 2 ** n)
        raise ErroreFonte(f"{url}: nessuna risposta dopo {tentativi} tentativi ({errore})") from errore
