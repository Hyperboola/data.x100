"""Identità visiva di aperidata (@aperidata).

Unica fonte di verità per colori e caratteri: le slide importano da qui,
così un cambio di palette si propaga ovunque.
"""
from pathlib import Path

import requests

# --- Colori -----------------------------------------------------------------
INCHIOSTRO = "#111418"   # testo, sfondo copertine
NEBBIA = "#EEF1F4"       # sfondo grafici
ARANCIO = "#F2542D"      # il dato che conta (uno per slide)
BLU = "#2A4D9B"          # confronto
GRIGIO_BARRE = "#A9B3C2" # barre neutre
GRIGIO_NOTA = "#5A6170"  # fonti, etichette secondarie

# --- Caratteri ----------------------------------------------------------------
FONTS_DIR = Path(__file__).parent / "fonts"
_GF = "https://raw.githubusercontent.com/google/fonts/main/ofl"
FONT_SOURCES = {
    "BricolageGrotesque.ttf": f"{_GF}/bricolagegrotesque/BricolageGrotesque%5Bopsz,wdth,wght%5D.ttf",
    "InstrumentSans.ttf": f"{_GF}/instrumentsans/InstrumentSans%5Bwdth,wght%5D.ttf",
    "IBMPlexMono-Regular.ttf": f"{_GF}/ibmplexmono/IBMPlexMono-Regular.ttf",
    "IBMPlexMono-Medium.ttf": f"{_GF}/ibmplexmono/IBMPlexMono-Medium.ttf",
}


def ensure_fonts() -> None:
    """Scarica i font (licenza OFL) se mancano."""
    FONTS_DIR.mkdir(exist_ok=True)
    for name, url in FONT_SOURCES.items():
        path = FONTS_DIR / name
        if not path.exists():
            # requests usa i certificati di certifi: evita gli errori SSL del Python di macOS
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            path.write_bytes(resp.content)


def font_path(name: str) -> Path:
    """Percorso di un file font del brand (es. "InstrumentSans.ttf"), scaricandolo se manca."""
    if name not in FONT_SOURCES:
        raise KeyError(f"Font sconosciuto: {name}")
    ensure_fonts()
    return FONTS_DIR / name

