#!/usr/bin/env python3
"""
check_missing_pubs.py

For every row in endo_funding_grants_combined.csv where `first_author` is
blank (i.e. the original matching process found no publication for that
grant), this script re-queries the Web of Science Starter API to double
check whether a publication actually exists that was simply missed.

It tries two search strategies per grant, in order:

  1. FUNDING-TEXT MATCH  (most reliable when it works)
     Many funders' grant numbers show up verbatim in the WoS "Funding
     Text" field, e.g. "MOP-142273", "PJT-165342". Since this dataset's
     `frn` column is a CIHR Funding Reference Number, we search FT= for
     that bare number. If your frn->grant-number mapping uses a known
     prefix, edit FRN_PREFIXES below to try prefixed variants too.

  2. PI + INSTITUTION + DATE WINDOW  (fallback, broader/noisier)
     Searches AU=<PI last name, first initial> AND OG=<institution>
     restricted to publication years spanning the grant's active period
     through ~3 years after the end date (grants often publish after
     they close). Because author-name matching is noisy, these results
     are NOT auto-accepted -- they're written out for manual review.

Usage:
    export WOS_API_KEY="a1681eb3e89ee5cea1b56554fce6b328d09b481e"
    pip install requests
    python3 check_missing_pubs.py endo_funding_grants_combined.csv results.csv

Output:
    results.csv - one row per (grant, candidate publication found), plus
                  rows with candidate_count=0 for grants where nothing
                  turned up under either strategy.
"""

import csv
import os
import sys
import time
import datetime
import requests

API_KEY = os.environ.get("WOS_API_KEY", "a1681eb3e89ee5cea1b56554fce6b328d09b481e")
BASE_URL = "https://api.clarivate.com/api/wos"
HEADERS = {"X-ApiKey": API_KEY, "Accept": "application/json"}

# If you know the grant-number prefix(es) used for these frn's, list them
# here and the script will also try "PREFIX-<frn>" and "PREFIX<frn>" in
# the funding-text search. Leave empty to search the bare number only.
FRN_PREFIXES = ["MOP", "PJT", "OG", "GAC"]

REQUEST_DELAY_SEC = 1.0   # be polite to the API / respect rate limits
YEARS_AFTER_END_TO_SEARCH = 3


def parse_date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def pi_search_name(pi_name):
    """Turn 'Paul Yong' into 'Yong P' for an AU= search."""
    parts = pi_name.strip().split()
    if len(parts) < 2:
        return pi_name.strip()
    last = parts[-1]
    first_initial = parts[0][0]
    return f"{last} {first_initial}"


def _as_list(x):
    """The Expanded API collapses single-item lists to a bare dict. Normalize."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def wos_search(query, limit=10):
    """Run a single WoS Expanded API search, return list of doc summaries."""
    params = {
        "databaseId": "WOS",
        "usrQuery": query,
        "count": limit,
        "firstRecord": 1,
    }
    try:
        resp = requests.get(BASE_URL, headers=HEADERS, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"    [error] request failed: {e}")
        return None, str(e)

    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"

    data = resp.json()
    recs = _as_list(
        data.get("Data", {}).get("Records", {}).get("Records", {}).get("REC")
    )

    out = []
    for rec in recs:
        uid = rec.get("UID", "")
        summary = rec.get("static_data", {}).get("summary", {})

        titles = _as_list(summary.get("titles", {}).get("title"))
        title = ""
        for t in titles:
            if t.get("type") == "item":
                title = t.get("content", "")
                break
        if not title and titles:
            title = titles[0].get("content", "")

        pub_year = summary.get("pub_info", {}).get("pubyear", "")

        names = _as_list(summary.get("names", {}).get("name"))
        first_author = ""
        for n in names:
            if n.get("role") == "author":
                first_author = n.get("display_name") or n.get("full_name") or ""
                break

        out.append({
            "wos_uid": uid,
            "title": title,
            "pub_year": pub_year,
            "first_author": first_author,
        })
    return out, None


def build_funding_text_queries(frn):
    frn = str(frn).strip()
    queries = [f'FT=("{frn}")']
    for prefix in FRN_PREFIXES:
        queries.append(f'FT=("{prefix}-{frn}")')
        queries.append(f'FT=("{prefix}{frn}")')
    return queries


def build_pi_query(pi_name, institution, start_date, end_date):
    au = pi_search_name(pi_name)
    py_start = start_date.year if start_date else 1990
    py_end = (end_date.year + YEARS_AFTER_END_TO_SEARCH) if end_date else datetime.date.today().year
    py_end = min(py_end, datetime.date.today().year)
    q = f'AU=("{au}") AND PY=({py_start}-{py_end})'
    return q


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 check_missing_pubs.py <input_csv> <output_csv>")
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]

    if not API_KEY:
        print("ERROR: set WOS_API_KEY environment variable (or hardcode API_KEY above).")
        sys.exit(1)

    with open(in_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    missing_rows = [r for r in rows if not r.get("first_author", "").strip()]
    # de-duplicate by frn in case the same grant appears multiple times
    seen_frn = {}
    for r in missing_rows:
        seen_frn.setdefault(r["frn"], r)
    missing_rows = list(seen_frn.values())

    print(f"Found {len(missing_rows)} grants with no matched publication (deduped by frn).")

    out_fields = [
        "frn", "pi_name", "institution", "grant_start_date", "grant_end_date",
        "search_strategy", "query_used", "candidate_wos_uid", "candidate_title",
        "candidate_pub_year", "candidate_first_author", "error",
    ]

    with open(out_path, "w", newline="", encoding="utf-8") as outf:
        writer = csv.DictWriter(outf, fieldnames=out_fields)
        writer.writeheader()

        for i, row in enumerate(missing_rows, 1):
            frn = row["frn"]
            pi_name = row["pi_name"]
            institution = row.get("institution", "")
            start_date = parse_date(row.get("grant_start_date", ""))
            end_date = parse_date(row.get("grant_end_date", ""))

            print(f"[{i}/{len(missing_rows)}] frn={frn} pi={pi_name}")

            found_any = False

            # --- Strategy 1: funding text match ---
            for q in build_funding_text_queries(frn):
                hits, err = wos_search(q, limit=5)
                time.sleep(REQUEST_DELAY_SEC)
                if err:
                    writer.writerow({
                        "frn": frn, "pi_name": pi_name, "institution": institution,
                        "grant_start_date": row.get("grant_start_date", ""),
                        "grant_end_date": row.get("grant_end_date", ""),
                        "search_strategy": "funding_text", "query_used": q,
                        "candidate_wos_uid": "", "candidate_title": "",
                        "candidate_pub_year": "", "candidate_first_author": "",
                        "error": err,
                    })
                    continue
                if hits:
                    found_any = True
                    for h in hits:
                        writer.writerow({
                            "frn": frn, "pi_name": pi_name, "institution": institution,
                            "grant_start_date": row.get("grant_start_date", ""),
                            "grant_end_date": row.get("grant_end_date", ""),
                            "search_strategy": "funding_text", "query_used": q,
                            "candidate_wos_uid": h["wos_uid"],
                            "candidate_title": h["title"],
                            "candidate_pub_year": h["pub_year"],
                            "candidate_first_author": h["first_author"],
                            "error": "",
                        })
                    # stop trying more prefix variants once we get a hit
                    break

            # --- Strategy 2: PI + date window fallback (always run, for
            #     manual review, even if funding-text already found something,
            #     since funding-text hits might be false positives / partial
            #     acknowledgments from co-authored grants) ---
            q2 = build_pi_query(pi_name, institution, start_date, end_date)
            hits2, err2 = wos_search(q2, limit=10)
            time.sleep(REQUEST_DELAY_SEC)
            if err2:
                writer.writerow({
                    "frn": frn, "pi_name": pi_name, "institution": institution,
                    "grant_start_date": row.get("grant_start_date", ""),
                    "grant_end_date": row.get("grant_end_date", ""),
                    "search_strategy": "pi_institution_fallback", "query_used": q2,
                    "candidate_wos_uid": "", "candidate_title": "",
                    "candidate_pub_year": "", "candidate_first_author": "",
                    "error": err2,
                })
            elif hits2:
                found_any = True
                for h in hits2:
                    writer.writerow({
                        "frn": frn, "pi_name": pi_name, "institution": institution,
                        "grant_start_date": row.get("grant_start_date", ""),
                        "grant_end_date": row.get("grant_end_date", ""),
                        "search_strategy": "pi_institution_fallback", "query_used": q2,
                        "candidate_wos_uid": h["wos_uid"],
                        "candidate_title": h["title"],
                        "candidate_pub_year": h["pub_year"],
                        "candidate_first_author": h["first_author"],
                        "error": "",
                    })

            if not found_any:
                writer.writerow({
                    "frn": frn, "pi_name": pi_name, "institution": institution,
                    "grant_start_date": row.get("grant_start_date", ""),
                    "grant_end_date": row.get("grant_end_date", ""),
                    "search_strategy": "none", "query_used": "",
                    "candidate_wos_uid": "", "candidate_title": "",
                    "candidate_pub_year": "", "candidate_first_author": "",
                    "error": "",
                })

    print(f"\nDone. Results written to {out_path}")
    print("NOTE: 'pi_institution_fallback' rows are candidates only -- "
          "review titles/years by hand to confirm relevance to the grant, "
          "since author-name searches can match unrelated people or papers.")


if __name__ == "__main__":
    main()
