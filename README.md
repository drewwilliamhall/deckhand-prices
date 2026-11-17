# TCG Deckhand - live prices

This folder keeps the prices on tcgdeckhand.net current.

`refresh_prices.py` reads three sources and writes one file, `prices.json`:

| Source | What it gives | How often | Needs |
|---|---|---|---|
| TCG API (tcgapi.dev) | TCGplayer market price for every card, printing and sealed product | once a day | `TCGAPI_KEY` |
| CardTrader API | lowest English near-mint listing | every 3 hours | `CARDTRADER_TOKEN` |
| Store feeds | each store in `stores.json`, read from its public Shopify product feed | every 3 hours | nothing |

The app (`app.html`) downloads `prices.json` each time the page opens. If the file can't be
reached within six seconds, the page falls back to the prices saved inside it.

## How it runs

GitHub Actions runs `.github/workflows/refresh-prices.yml` on a schedule and publishes the
result to the `live` branch of this repository. The page reads it from:

    https://raw.githubusercontent.com/drewwilliamhall/deckhand-prices/live/prices.json

The two keys are stored as repository secrets (Settings > Secrets and variables > Actions):
`TCGAPI_KEY` and `CARDTRADER_TOKEN`. They are never written to a file in the repository.

To run it by hand: Actions tab > Refresh prices > Run workflow.

## Running it on your own computer

    python refresh_prices.py --sources ct,shops

Put the keys in a file named `.env` next to the script (`TCGAPI_KEY=...` and
`CARDTRADER_TOKEN=...`). That file is ignored by git - keep it that way.

## Things worth knowing

- The free TCG API plan allows 100 requests a day; a full pull uses about 92. If the allowance
  runs out, the script keeps yesterday's numbers for the sets it didn't reach and catches up
  the next day, oldest first.
- Store prices are only accepted for the standard printing of a card, in stock, near mint and
  in English. Promos, alternate arts, graded cards and other games are skipped, and a price far
  out of line with the TCGplayer price is set aside rather than shown.
- Store prices are converted to US dollars at the day's exchange rate.
- If a source fails, its previous prices are kept and the other sources still update.
- `stores.json` lists the stores to read. Add a store by adding a line with its id (the same id
  as in `vendors.csv`), web address and currency.
- `embed_snapshot.py` refreshes the fallback prices saved inside the page:
  `python embed_snapshot.py prices.json ../site/app.html`
