"""Passo 1 · Dai sottotemi del calendario ai dataset ISTAT candidati.

    python -m pipeline.categorie                    # quanti dataset ha ogni sottotema
    python -m pipeline.categorie nascite            # i dataset di un sottotema, dal più aggiornato
    python -m pipeline.categorie --cerca natalit    # codici di categoria, per il calendario
    python -m pipeline.categorie --dataset 25_74_DF_DCIS_NATI2_1   # dimensioni e codici di un dataset
"""
from __future__ import annotations

import argparse
import logging

import pandas as pd

from pipeline.calendario import carica_calendario
from sources.istat import Istat


def candidati(sottotema: str, cal: dict, istat: Istat | None = None) -> pd.DataFrame:
    """Dataset del sottotema, esclusi i temi delicati, dal più aggiornato al meno."""
    istat = istat or Istat()
    codici = set(cal["sottotemi"][sottotema])
    esclusi = set(cal["esclusi"]["categorie"])
    albero = istat.categorie()
    if spariti := codici - set().union(*albero["antenati"].map(set)):  # ISTAT ha rinominato o tolto una categoria
        logging.getLogger(__name__).warning("%s: categorie non più nel catalogo ISTAT %s (cercale con --cerca)",
                                            sottotema, sorted(spariti))
    delicati = set(albero.loc[albero["antenati"].map(lambda a: bool(esclusi & set(a))), "dataset"])
    scelti = albero[albero["antenati"].map(lambda a: bool(codici & set(a))) & ~albero["dataset"].isin(delicati)]
    scelti = scelti.drop_duplicates("dataset")
    dataset = istat.dataset()[["id", "last_update"]]
    out = scelti.merge(dataset, left_on="dataset", right_on="id", how="inner")  # solo quelli scaricabili
    out = out[~out["dataset"].str.upper().str.startswith("DF_BULK")]  # archivi in blocco: niente struttura SDMX
    return out.sort_values("last_update", ascending=False, na_position="last")[
        ["dataset", "titolo", "percorso", "last_update"]].reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Esplora i dataset ISTAT per sottotema")
    parser.add_argument("sottotema", nargs="?")
    parser.add_argument("--cerca", metavar="TESTO", help="cerca codici di categoria")
    parser.add_argument("--dataset", metavar="ID", help="mostra dimensioni e codici di un dataset")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cal, istat = carica_calendario(), Istat()

    with pd.option_context("display.max_colwidth", 80, "display.width", 180, "display.max_rows", 300):
        if args.cerca:
            albero = istat.categorie()
            righe = albero[albero["percorso"].str.contains(args.cerca, case=False, regex=False)]
            righe = righe.assign(categoria=righe["antenati"].str[-1]).groupby(["categoria", "percorso"]).size()
            print(righe.rename("dataset").reset_index().to_string(index=False))
        elif args.dataset:
            codici, meta = istat.struttura(args.dataset)
            print(f"{meta['nome']} · aggiornato {str(meta.get('last_update') or '?')[:10]}")
            for (pos, dim, nome), g in codici.groupby(["position", "dimension", "dimension_name"]):
                esempi = ", ".join(f"{c}={n}" for c, n in zip(g["code"].head(8), g["name"].head(8)))
                print(f"\n{pos}. {dim} – {nome} ({len(g)} codici)\n   {esempi}{' …' if len(g) > 8 else ''}")
        elif args.sottotema:
            c = candidati(args.sottotema, cal, istat)
            c["last_update"] = c["last_update"].dt.strftime("%Y-%m-%d")
            print(f"{len(c)} dataset per '{args.sottotema}'\n")
            print(c[["last_update", "dataset", "titolo"]].to_string(index=False))
        else:
            for sott in cal["sottotemi"]:
                n = len(candidati(sott, cal, istat)) if cal["sottotemi"][sott] else 0
                print(f"{cal['giorno_di'][sott]:<10} {sott:<24} {n:>4} dataset")


if __name__ == "__main__":
    main()
