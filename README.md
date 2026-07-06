# isinbul

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/buxita)

This project is built in my spare time and is continuously maintained and
extended. If it's useful to you, I'd be thrilled about a small donation!
https://ko-fi.com/buxita

Fetch and look up ISIN codes for Turkish stock companies listed on Borsa
Istanbul (BIST).

`pykap` and similar tools give you the list of BIST companies but not their
ISINs. `isinbul` fills that gap by combining two sources:

- **Company list** (ticker, name, city) via [`borsapy`](https://pypi.org/project/borsapy/), sourced from KAP (Kamuyu Aydınlatma Platformu).
- **ISIN codes** via [isinturkiye.com.tr](https://www.isinturkiye.com.tr), Turkey's official ISIN-issuing authority, with an improved IDF-weighted issuer matching on top of `borsapy`'s lookup.

Current coverage: **598 of 781** BIST-listed companies resolved to a real,
verified ISIN. The rest are mostly non-equity entities (factoring/leasing/
consumer-finance companies, investment banks without listed shares) that
structurally have no stock ISIN, plus a handful of genuine data gaps in the
source itself.

## Install

```bash
pip install -r requirements.txt
```

Requires Python 3.10+.

## Usage

```bash
# Fetch ALL companies + ISIN into a CSV (safe to re-run, skips what's done)
python isinbul.py fetch

# Categorize an existing CSV (STOCK / DUPLICATE / NON_EQUITY / UNRESOLVED)
# and write a clean, ISIN-only isin_liste_aktien.csv
python isinbul.py clean

# Retry unresolved companies with improved issuer matching
python isinbul.py resolve

# Look up a single company by ticker, name fragment, or ISIN (in either direction)
python isinbul.py lookup THYAO
python isinbul.py lookup "hava yollari"
python isinbul.py lookup TRATHYAO91M5
```

`lookup` reads from a local `isin_liste.csv` if present (instant, no network)
and falls back to a live query otherwise.

## Notes

- Uses `truststore` to fall back to the OS certificate store if the bundled
  `certifi` CAs fail to verify (common behind corporate proxies/antivirus
  TLS inspection).
- Be a good citizen: `fetch` and `resolve` default to a small delay between
  requests against the public isinturkiye.com.tr API.
