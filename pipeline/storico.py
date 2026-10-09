"""La memoria della pagina: cosa è già uscito.

    python -m pipeline.storico              # riepilogo e ultimi post
    python -m pipeline.storico nascite      # tutti i post di un sottotema
    python -m pipeline.storico --consumo    # quanti dataset restano nuovi, per sottotema
"""
from __future__ import annotations

import argparse
import json
import logging

import pandas as pd

from pipeline.calendario import carica_calendario
from pipeline.run import STORICO


def carica() -> pd.DataFrame:
    voci = json.loads(STORICO.read_text()) if STORICO.exists() else []
    return pd.DataFrame(voci, columns=["data", "slot", "sottotema", "flow", "periodo", "numero", "domanda", "serie"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Cosa è già stato pubblicato")
    parser.add_argument("sottotema", nargs="?")
    parser.add_argument("--consumo", action="store_true", help="dataset ancora mai usati per ogni sottotema")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    st = carica()
    if st.empty:
        print("Nessun post ancora pubblicato.")
        return
    with pd.option_context("display.max_colwidth", 60, "display.width", 180, "display.max_rows", 500):
        if args.consumo:
            from pipeline.categorie import candidati

            cal, usati = carica_calendario(), set(st["flow"])
            for sott, codici in cal["sottotemi"].items():
                if codici:
                    c = candidati(sott, cal)
                    nuovi = len(set(c["dataset"]) - usati)
                    print(f"{cal['giorno_di'][sott]:<10} {sott:<24} {nuovi:>4} nuovi su {len(c):>4}")
        elif args.sottotema:
            print(st[st["sottotema"] == args.sottotema][["data", "slot", "domanda", "numero", "periodo"]]
                  .to_string(index=False))
        else:
            print(f"{len(st)} post, {st['flow'].nunique()} dataset diversi, dal {st['data'].min()} al {st['data'].max()}\n")
            per = st.groupby("sottotema").agg(post=("data", "size"), ultimo=("data", "max")).sort_values("ultimo")
            print(per.to_string(), "\n")
            print(st.tail(10)[["data", "slot", "sottotema", "domanda", "numero"]].to_string(index=False))


if __name__ == "__main__":
    main()
