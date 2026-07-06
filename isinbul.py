"""
isinbul - Fetches and looks up ISIN codes for Turkish stock companies
listed on Borsa Istanbul (BIST).

Data sources (via the `borsapy` library):
  - Company list (ticker, name, city):  KAP (Kamuyu Aydinlatma Platformu,
                                         Turkey's public disclosure platform)
  - ISIN per ticker / ticker per ISIN:  isinturkiye.com.tr (Turkey's
                                         official ISIN-issuing authority)

Four modes:

  fetch    Fetches ALL companies + ISIN and writes them to a CSV file as it
           goes. If interrupted (network error, rate limit, Ctrl+C), just
           run it again - missing entries are picked up automatically.

  clean    Categorizes an existing CSV (STOCK/DUPLICATE/NON_EQUITY/
           UNRESOLVED) and additionally writes a clean, stock-only list.

  resolve  Retries companies marked UNRESOLVED with an improved,
           IDF-weighted issuer match against isinturkiye.com.tr.

  lookup   Targeted single lookup in either direction:
             - Ticker       (e.g. THYAO)        -> name + ISIN
             - Name snippet (e.g. hava)         -> matching companies + ISIN
             - ISIN         (e.g. TRATHYAO91M5) -> ticker + name
           Checks an existing local CSV first (instant, no network), falls
           back to a live query if needed.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path

try:
    # Falls back to the OS certificate store (Windows/macOS/Linux) instead
    # of the bundled certifi CAs. Helps when a corporate firewall or
    # antivirus product intercepts TLS with its own certificate
    # ("CERTIFICATE_VERIFY_FAILED: unable to get local issuer").
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import borsapy as bp
import httpx
from tqdm import tqdm

ISINTURKIYE_BASE = "https://www.isinturkiye.com.tr/v17/tvs/isin/portal/bff/tvs/isin/portal/public"
ISINTURKIYE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.isinturkiye.com.tr",
    "Referer": "https://www.isinturkiye.com.tr/v17/tvs/isin/portal/bff/index.html",
}

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

FIELDNAMES = ["ticker", "name", "city", "isin", "status"]

TR_MAP = str.maketrans({
    "İ": "I", "I": "I", "ı": "I",
    "Ş": "S", "ş": "S",
    "Ğ": "G", "ğ": "G",
    "Ü": "U", "ü": "U",
    "Ö": "O", "ö": "O",
    "Ç": "C", "ç": "C",
})


def normalize(text: str) -> str:
    """Rough normalization of Turkish special characters for matching."""
    return text.translate(TR_MAP).upper()


def is_isin(query: str) -> bool:
    return bool(ISIN_RE.match(query.strip().upper()))


# Companies whose name contains one of these legal-form/business-activity
# terms typically have no exchange-traded common stock at all - they're
# only disclosure-obligated at KAP because of bond/sukuk issuances (e.g.
# factoring, leasing, and securitization companies, or pure brokerages).
NON_EQUITY_KEYWORDS = [
    "FAKTORING", "FAKTORİNG",
    "FINANSAL KIRALAMA", "FİNANSAL KİRALAMA",
    "VARLIK KIRALAMA", "VARLIK YONETIM",
    "MENKUL DEGERLER", "MENKUL KIYMETLER",
    "SUKUK", "ISSUANCE",
    "FINANSMAN",  # consumer/auto finance companies (e.g. Koc Finansman)
    "YATIRIM BANKASI",  # Turkish investment banks without a BIST stock listing
    "FILO KIRALAMA", "ARAC KIRALAMA",  # fleet/car rental companies
    "GENEL MUDURLUGU", "BAKANLIGI",  # government agencies (e.g. the Mint)
    "KAP TEST",  # KAP's own test entry, not a real company
]
NON_EQUITY_KEYWORDS_NORM = [normalize(k) for k in NON_EQUITY_KEYWORDS]


def classify(rows: dict[str, dict]) -> None:
    """Sets/updates the 'status' column for every row (in place).

    STOCK       - ISIN present
    DUPLICATE   - no ISIN, but the same company already has one under a
                  different ticker (e.g. a parallel bond-program code)
    NON_EQUITY  - no ISIN, name suggests a factoring/leasing/sukuk SPV or
                  brokerage house (no traded common stock)
    UNRESOLVED  - no ISIN and none of the above patterns match; needs a
                  manual look
    """
    name_to_ticker: dict[str, str] = {}
    for ticker, row in rows.items():
        if row.get("isin"):
            name_to_ticker[row["name"]] = ticker

    for ticker, row in rows.items():
        if row.get("isin"):
            row["status"] = "STOCK"
        elif row["name"] in name_to_ticker:
            row["status"] = f"DUPLICATE (see {name_to_ticker[row['name']]})"
        elif any(kw in normalize(row["name"]) for kw in NON_EQUITY_KEYWORDS_NORM):
            row["status"] = "NON_EQUITY"
        else:
            row["status"] = "UNRESOLVED"


# --------------------------------------------------------------------------
# resolve: better matching for companies where borsapy's fuzzy match
# (KAP name -> single best hit) picked the wrong issuer.
#
# Root cause (traced through MGROS/Migros): isinturkiye.com.tr lists
# MULTIPLE issuer entries with an identical core name for many companies
# ("MIGROS TURK T.A.S." and "MIGROS TICARET ANONIM SIRKETI"), both of
# which score equally high on a plain keyword match. borsapy takes the
# FIRST hit and never checks whether its security list actually contains
# the ticker being looked up. Here, ALL candidates are tried in turn and
# verified live instead (the ticker must actually show up in that
# issuer's security list).
# --------------------------------------------------------------------------

STOPWORDS = {
    "VE", "A", "AS", "AO", "TAS", "ANONIM", "SIRKETI", "SIRKET",
    "TURKIYE", "TURK", "HOLDING", "SANAYI", "SANAYII", "TICARET",
    "LTD", "STI", "SAN", "TIC", "GRUBU", "GRUP",
}


def keywords(name: str) -> set[str]:
    tokens = re.sub(r"[.,\-'\"]+", " ", normalize(name)).split()
    return {t for t in tokens if t not in STOPWORDS and len(t) > 2}


def fetch_issuer_list(client: httpx.Client) -> list[dict]:
    """One-off fetch of ALL issuers registered with isinturkiye.com.tr
    (stocks, funds, bonds, ... - roughly 2000 entries with code + name)."""
    resp = client.post(f"{ISINTURKIYE_BASE}/isinSirketListe", json={}, headers=ISINTURKIYE_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("resultList") or []


def issuer_name(issuer: dict) -> str:
    srk_ad = issuer.get("srkAd", "")
    return srk_ad.split(" - ", 1)[1] if " - " in srk_ad else srk_ad


def build_idf(issuers: list[dict]) -> dict[str, float]:
    """Frequency of every keyword across all ~2000 issuer names. Generic
    industry terms (e.g. TEKSTIL, ELEKTRONIK) show up often and get a low
    weight this way; unique brand names (e.g. KORDSA) get a high weight -
    otherwise a random neighbor from the same industry would outscore the
    actual match (see the docstring above)."""
    df: dict[str, int] = {}
    for issuer in issuers:
        for tok in keywords(issuer_name(issuer)):
            df[tok] = df.get(tok, 0) + 1
    return {tok: 1.0 / count for tok, count in df.items()}


def candidate_ihrac_kods(kap_name: str, issuers: list[dict], idf: dict[str, float], top_n: int = 6) -> list[str]:
    """Ranks all issuers by IDF-weighted keyword overlap with the KAP name
    (instead of raw overlap size - see build_idf)."""
    target = keywords(kap_name)
    if not target:
        return []
    scored = []
    for issuer in issuers:
        candidate = keywords(issuer_name(issuer))
        common = target & candidate
        if not common:
            continue
        union = target | candidate
        num = sum(idf.get(t, 1.0) for t in common)
        den = sum(idf.get(t, 1.0) for t in union)
        scored.append((num / den if den else 0.0, issuer["srkKod"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [kod for score, kod in scored[:top_n] if score > 0.15]


def try_ihrac_kod(ticker: str, ihrac_kod: str, client: httpx.Client) -> str | None:
    """Queries all securities of one issuer and looks for the share with a
    matching exchange ticker."""
    payload = {"isinKod": "", "ihracKod": ihrac_kod, "kategori": "", "menkulTurKod": ""}
    resp = client.post(f"{ISINTURKIYE_BASE}/isinListele", json=payload, headers=ISINTURKIYE_HEADERS, timeout=15)
    resp.raise_for_status()
    for item in resp.json().get("resultList") or []:
        borsa_kodu = (item.get("borsaKodu") or "").split(" - ")[0].strip()
        menkul_tur = item.get("menkulTur", "")
        if borsa_kodu.upper() == ticker.upper() and ("PAY" in menkul_tur or "Hisse" in menkul_tur):
            return item.get("isinKod")
    return None


def advanced_resolve(
    ticker: str, kap_name: str, issuers: list[dict], idf: dict[str, float], client: httpx.Client
) -> str | None:
    for ihrac_kod in candidate_ihrac_kods(kap_name, issuers, idf):
        isin = try_ihrac_kod(ticker, ihrac_kod, client)
        if isin:
            return isin
    return None


def cmd_resolve(args: argparse.Namespace) -> int:
    output_path = Path(args.output)
    results = load_existing(output_path)
    if not results:
        print(f"{output_path} not found or empty.", file=sys.stderr)
        return 1

    classify(results)
    todo = [row for row in results.values() if row["status"] == "UNRESOLVED"]
    print(f"{len(todo)} unresolved companies, trying improved matching ...")

    client = httpx.Client()
    print("Loading issuer list from isinturkiye.com.tr ...")
    issuers = fetch_issuer_list(client)
    idf = build_idf(issuers)
    print(f"{len(issuers)} issuers loaded.")

    recovered = 0
    try:
        for row in tqdm(todo, desc="Resolving", unit="company"):
            try:
                isin = advanced_resolve(row["ticker"], row["name"], issuers, idf, client)
            except Exception as exc:
                tqdm.write(f"  Error for {row['ticker']}: {exc}")
                isin = None
            if isin:
                row["isin"] = isin
                recovered += 1
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nInterrupted - saving results so far.")
    finally:
        classify(results)
        write_all(output_path, results)

    print(f"\n{recovered}/{len(todo)} additionally resolved and saved to {output_path}.")
    still_open = [r["ticker"] for r in results.values() if r["status"] == "UNRESOLVED"]
    if still_open:
        print(f"Still unresolved ({len(still_open)}): {', '.join(sorted(still_open))}")
    return 0


# --------------------------------------------------------------------------
# fetch: complete list
# --------------------------------------------------------------------------

def load_existing(output_path: Path) -> dict[str, dict]:
    if not output_path.exists():
        return {}
    with output_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return {row["ticker"]: row for row in reader}


def write_all(output_path: Path, rows: dict[str, dict]) -> None:
    tmp_path = output_path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for ticker in sorted(rows):
            row = rows[ticker]
            writer.writerow({name: row.get(name, "") for name in FIELDNAMES})
    tmp_path.replace(output_path)


def cmd_fetch(args: argparse.Namespace) -> int:
    output_path = Path(args.output)
    results = load_existing(output_path)
    if results:
        print(f"{len(results)} existing entries loaded from {output_path}, will be skipped.")

    print("Loading company list from KAP via borsapy ...")
    companies_df = bp.companies()
    if companies_df is None or companies_df.empty:
        print("Could not load company list.", file=sys.stderr)
        return 1

    companies_df = companies_df.drop_duplicates(subset="ticker").sort_values("ticker")
    if args.limit:
        companies_df = companies_df.head(args.limit)

    todo = [
        row for row in companies_df.to_dict("records")
        if not results.get(row["ticker"], {}).get("isin")
    ]
    print(f"{len(companies_df)} companies total, {len(todo)} of them still without an ISIN.")

    failed: list[str] = []
    try:
        for row in tqdm(todo, desc="Fetching ISIN", unit="company"):
            ticker = row["ticker"]
            isin = None
            try:
                isin = bp.Ticker(ticker).isin
            except Exception as exc:  # a single company's network/parsing error shouldn't abort the whole run
                tqdm.write(f"  Error for {ticker}: {exc}")

            if not isin:
                failed.append(ticker)

            results[ticker] = {
                "ticker": ticker,
                "name": row.get("name", ""),
                "city": row.get("city", ""),
                "isin": isin or "",
            }

            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nInterrupted - saving results so far.")
    finally:
        classify(results)
        write_all(output_path, results)

    ok = sum(1 for r in results.values() if r["isin"])
    print(f"\nDone: {ok}/{len(results)} companies with ISIN saved to {output_path}.")
    if failed:
        print(f"No ISIN found for {len(failed)} companies: {', '.join(failed[:20])}"
              + (" ..." if len(failed) > 20 else ""))
        print("Just run the script again to retry only these.")
        print(f"See the 'status' column in {output_path} for a breakdown "
              f"(or run: 'python isinbul.py clean -o {output_path}').")

    return 0


# --------------------------------------------------------------------------
# clean: categorize an existing CSV and emit a clean stock-only list
# --------------------------------------------------------------------------

def cmd_clean(args: argparse.Namespace) -> int:
    output_path = Path(args.output)
    results = load_existing(output_path)
    if not results:
        print(f"{output_path} not found or empty.", file=sys.stderr)
        return 1

    classify(results)
    write_all(output_path, results)

    counts: dict[str, int] = {}
    for row in results.values():
        status = row["status"].split(" (")[0]
        counts[status] = counts.get(status, 0) + 1

    print(f"{output_path} updated ({len(results)} rows):")
    for status in ("STOCK", "DUPLICATE", "NON_EQUITY", "UNRESOLVED"):
        if status in counts:
            print(f"  {status:<12} {counts[status]}")

    stocks_path = Path(args.stocks_output)
    stock_rows = [row for row in results.values() if row["status"] == "STOCK"]
    with stocks_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "name", "city", "isin"], extrasaction="ignore")
        writer.writeheader()
        for row in sorted(stock_rows, key=lambda r: r["ticker"]):
            writer.writerow(row)
    print(f"Clean stock list ({len(stock_rows)} companies) written to {stocks_path}.")

    unresolved = [r["ticker"] for r in results.values() if r["status"] == "UNRESOLVED"]
    if unresolved:
        print(f"\nNeeds a manual look ({len(unresolved)}): {', '.join(sorted(unresolved))}")

    return 0


# --------------------------------------------------------------------------
# lookup: targeted single lookup in either direction
# --------------------------------------------------------------------------

def lookup_isin_live(isin: str) -> dict | None:
    """ISIN -> {ticker, name} via a live query against isinturkiye.com.tr."""
    provider = bp.Ticker("THYAO")._get_isin_provider()  # just to get at the httpx client
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.isinturkiye.com.tr",
        "Referer": "https://www.isinturkiye.com.tr/v17/tvs/isin/portal/bff/index.html",
    }
    payload = {"isinKod": isin, "ihracKod": "", "kategori": "", "menkulTurKod": ""}
    resp = provider._client.post(
        "https://www.isinturkiye.com.tr/v17/tvs/isin/portal/bff/tvs/isin/portal/public/isinListele",
        json=payload, headers=headers, timeout=15,
    )
    resp.raise_for_status()
    result_list = resp.json().get("resultList") or []
    for item in result_list:
        borsa_kodu = item.get("borsaKodu") or ""
        if " - " in borsa_kodu:
            ticker, name = borsa_kodu.split(" - ", 1)
            return {"ticker": ticker.strip(), "name": name.strip(), "isin": item.get("isinKod", isin)}
    return None


def cmd_lookup(args: argparse.Namespace) -> int:
    query = args.query.strip()
    csv_path = Path(args.csv)
    rows = list(load_existing(csv_path).values()) if csv_path.exists() else []

    if is_isin(query):
        isin = query.upper()
        for row in rows:
            if row.get("isin", "").upper() == isin:
                print(f"{row['ticker']}\t{row['name']}\t{row['city']}\t{isin}")
                return 0
        print(f"'{isin}' not found in {csv_path}, querying isinturkiye.com.tr live ...")
        try:
            hit = lookup_isin_live(isin)
        except Exception as exc:
            print(f"Live query failed: {exc}", file=sys.stderr)
            return 1
        if hit:
            print(f"{hit['ticker']}\t{hit['name']}\t\t{hit['isin']}")
            return 0
        print("No company found with this ISIN.")
        return 1

    # Ticker or name search
    query_norm = normalize(query)
    matches = [
        row for row in rows
        if query_norm == normalize(row["ticker"])
        or query_norm in normalize(row["name"])
    ]
    if not matches and not rows:
        print(f"No local CSV found ({csv_path}), querying KAP live ...")
        companies_df = bp.companies()
        matches = [
            row for row in companies_df.to_dict("records")
            if query_norm == normalize(row["ticker"]) or query_norm in normalize(row["name"])
        ]
        for row in matches:
            row.setdefault("isin", "")

    if not matches:
        print(f"No company found for '{query}'.")
        return 1

    for row in matches:
        isin = row.get("isin") or ""
        if not isin:
            try:
                isin = bp.Ticker(row["ticker"]).isin or ""
            except Exception:
                isin = ""
        print(f"{row['ticker']}\t{row['name']}\t{row.get('city', '')}\t{isin}")

    return 0


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch", help="Fetch all companies + ISIN and save to CSV")
    p_fetch.add_argument("-o", "--output", default="isin_liste.csv",
                          help="Path to the output file (default: isin_liste.csv)")
    p_fetch.add_argument("--delay", type=float, default=0.5,
                          help="Delay in seconds between requests (default: 0.5)")
    p_fetch.add_argument("--limit", type=int, default=None,
                          help="Only query the first N companies (for testing)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_clean = sub.add_parser("clean", help="Categorize an existing CSV (stock/duplicate/non-equity)")
    p_clean.add_argument("-o", "--output", default="isin_liste.csv",
                          help="CSV from 'fetch' to clean up (default: isin_liste.csv)")
    p_clean.add_argument("--stocks-output", default="isin_liste_aktien.csv",
                          help="Output file for the clean stock-only list (default: isin_liste_aktien.csv)")
    p_clean.set_defaults(func=cmd_clean)

    p_resolve = sub.add_parser("resolve", help="Try improved matching for companies marked UNRESOLVED")
    p_resolve.add_argument("-o", "--output", default="isin_liste.csv",
                            help="CSV from 'fetch'/'clean' to work on (default: isin_liste.csv)")
    p_resolve.add_argument("--delay", type=float, default=0.3,
                            help="Delay in seconds between requests (default: 0.3)")
    p_resolve.set_defaults(func=cmd_resolve)

    p_lookup = sub.add_parser("lookup", help="Look up a single company by ticker/name/ISIN")
    p_lookup.add_argument("query", help="Ticker (THYAO), company name/snippet (hava), or ISIN (TRATHYAO91M5)")
    p_lookup.add_argument("--csv", default="isin_liste.csv",
                           help="Existing CSV from 'fetch' as a fast offline source (default: isin_liste.csv)")
    p_lookup.set_defaults(func=cmd_lookup)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
