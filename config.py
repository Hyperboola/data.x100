"""Configurazione condivisa della pipeline."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Tutti i dati stanno sotto DATA_DIR; in cloud basterà cambiare questa variabile
DATA_DIR = Path(os.environ.get("DATA_X100_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"