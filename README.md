# Data% · pipeline

Dai dati ISTAT ai caroselli Instagram di **Data%** (@data.x100).
I numeri li calcola il codice; la scelta della storia e i testi li fa Claude.

## Il giro

Con `usa_claude = false` (il default) la pipeline non usa intelligenza artificiale:

1. **categorie** — dal sottotema del giorno ai dataset ISTAT candidati
2. **scegli** — il dataset più recente non tecnico; la serie con regole (totali, indicatore più vicino al titolo, macro-aree per i confronti)
3. **racconta** — fatti calcolati dal codice, grafico preferito da `[racconto]`, testi a modello
4. **disegna** — le slide JPEG
5. **redazione** — nel browser scrivi i testi, scegli grafico e serie, approvi

Con `usa_claude = true` Claude sceglie dataset e serie e scrive la storia, con un revisore automatico.
In redazione il pulsante "Suggerisci" chiede a Claude un testo per un campo, con il modello leggero
indicato in `modello_claude` (default "haiku").

## La settimana tipo

```bash
python -m pipeline.run --settimana      # sabato o domenica: prepara la settimana dopo (lun-dom)
python -m pipeline.redazione            # scrivi, correggi, approva (si apre nel browser)
```

Poi i post approvati si programmano su Instagram (per ora con Meta Business Suite).

## Struttura

```
data_x100/
├── calendario.toml   giorni, temi, sottotemi con le categorie ISTAT, regole
├── preferiti.toml    serie scelte a mano, pubblicate il sabato
├── storico.json      tutto ciò che è già uscito (si crea al primo post)
├── prompt/           i testi per Claude: si migliorano senza toccare il codice
├── templates/        copertina, grafico, chiusura (HTML + CSS)
├── brand/theme.py    colori e font
├── sources/          rete.py (richieste gentili) · istat.py (ISTAT)
├── pipeline/         run · categorie · scegli · racconta · disegna · redazione · storico · claude · calendario · periodi
├── config.py
├── data/             cache e storico (non versionato)
└── output/           i post, un giorno per cartella (non versionato):
    └── AAAA/MM/GG/
        ├── post/      slide del carosello (4:5)
        ├── reel/      le stesse slide in verticale (9:16)
        ├── video/     il video del grafico (MP4)
        └── altri/     rapporto, dati, didascalia e riepilogo.md
```

## Comandi

```bash
python -m pipeline.run                         # i post di oggi
python -m pipeline.ricognizione                # una volta (la sera): legge tutti i dataset del calendario
python -m pipeline.ricognizione --riepilogo    # quanti dataset freschi e mai usati ci sono, per sottotema
python -m pipeline.run --settimana             # i post della prossima settimana (lun-dom), da qualunque giorno
python -m pipeline.run --settimana --settimane 2   # le prossime due settimane
python -m pipeline.run --settimana --rifai     # rifà anche i giorni già preparati
python -m pipeline.redazione                   # la redazione nel browser
python -m pipeline.run --giorno lunedi         # come se fosse lunedì
python -m pipeline.run --serie nati_italia     # una serie preferita (--forza se il dato è vecchio)

python -m pipeline.redazione                   # rivedi e correggi i post di oggi nel browser

python -m pipeline.storico                     # cosa è già uscito
python -m pipeline.storico --consumo           # quanti dataset restano nuovi per sottotema

python -m pipeline.categorie                   # dataset per ogni sottotema
python -m pipeline.categorie nascite           # i candidati di un sottotema
python -m pipeline.categorie --cerca natalit   # codici di categoria per il calendario
python -m pipeline.categorie --dataset <id>    # dimensioni e codici di un dataset
```

## Fonti e licenza

- **SDMXWS** è il canale ufficiale di ISTAT per l'accesso automatico (limite: 5 richieste
  al minuto per IP; noi una ogni 20 secondi). Il backend del sito IstatData fa da riserva
  e fornisce l'albero delle categorie (una richiesta a settimana).
- I dati ISTAT sono rilasciati con licenza **CC BY 4.0**: ogni post cita la fonte, indica
  l'elaborazione e il link alla licenza. Il logo ISTAT non va usato.

## Installazione

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Serve anche Claude Code (`claude --version`) collegato al proprio abbonamento.

Ogni post esce in tre forme: le slide del post (4:5), le stesse slide per i reel (9:16) e un **video MP4 del grafico** che si costruisce periodo dopo periodo (`*_video.mp4`, 12 secondi, per reel e TikTok). Il video usa gli stessi dati della slide e si rigenera con "Salva e rigenera" in redazione. Serve `imageio-ffmpeg` (è in requirements.txt); i grafici a più linee e la ciambella non hanno il video.
