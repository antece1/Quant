#!/usr/bin/env python3
"""
edgar13f.py — SEC EDGAR 13F / 13D-G scraper.

Pulls primary-source filings straight from sec.gov so a claimed portfolio table
can be checked line by line against what was actually filed.

Usage:
    python3 edgar13f.py search "situational awareness"
    python3 edgar13f.py filings 0002045724
    python3 edgar13f.py holdings 0002045724                 # latest 13F-HR
    python3 edgar13f.py holdings 0002045724 --period 2026-03-31
    python3 edgar13f.py sc13 0002045724                     # SC 13D/G filings
    python3 edgar13f.py verify claims.json --cik 0002045724 --period 2026-03-31

SEC fair-access rules: identify yourself in User-Agent and stay under 10 req/s.
Override the UA with the EDGAR_UA environment variable.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict

UA = os.environ.get("EDGAR_UA", "Independent Research Project research@example.com")
MIN_INTERVAL = 0.15  # ~7 req/s, under SEC's 10/s ceiling
_last_call = 0.0


def get(url: str, tries: int = 3) -> bytes:
    """Throttled GET with the User-Agent EDGAR requires."""
    global _last_call
    for attempt in range(tries):
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept-Encoding": "gzip, deflate",
                "Host": urllib.parse.urlsplit(url).netloc,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                _last_call = time.time()
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip

                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            _last_call = time.time()
            if e.code in (403, 429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


import urllib.parse  # noqa: E402  (needed by get())


# ---------------------------------------------------------------- filer lookup

def search_filers(name: str) -> list[dict]:
    """Resolve a manager name to CIK(s) via EDGAR company search."""
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&company={urllib.parse.quote_plus(name)}&type=13F&dateb=&owner=include&count=40"
    )
    text = get(url).decode("utf-8", "replace")
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        cells = [
            html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if len(cells) >= 2 and re.fullmatch(r"\d{10}", cells[0]):
            out.append({"cik": cells[0], "name": cells[1], "state": cells[2] if len(cells) > 2 else ""})
    # Single exact hit redirects straight to the company page
    if not out:
        m = re.search(r"CIK=(\d{10})", text)
        n = re.search(r"companyName\">([^<]+)", text)
        if m:
            out.append({"cik": m.group(1), "name": html.unescape(n.group(1)).strip() if n else name, "state": ""})
    return out


@dataclass
class Filing:
    form: str
    accession: str
    filing_date: str
    period: str
    primary_doc: str

    @property
    def dir_url(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/{self.accession.replace('-', '')}"

    cik: str = ""

    @property
    def index_url(self) -> str:
        return f"{self.dir_url}/{self.accession}-index.htm"


def list_filings(cik: str, forms: tuple[str, ...] | None = None) -> list[Filing]:
    """All filings for a CIK from the submissions API, newest first."""
    cik10 = str(int(cik)).zfill(10)
    data = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik10}.json"))
    recent = data["filings"]["recent"]
    rows: list[Filing] = []

    def absorb(block: dict):
        for i in range(len(block["accessionNumber"])):
            form = block["form"][i]
            if forms and not any(form.startswith(f) for f in forms):
                continue
            rows.append(
                Filing(
                    form=form,
                    accession=block["accessionNumber"][i],
                    filing_date=block["filingDate"][i],
                    period=block.get("reportDate", [""] * (i + 1))[i] or "",
                    primary_doc=block["primaryDocument"][i],
                    cik=cik10,
                )
            )

    absorb(recent)
    for extra in data["filings"].get("files", []):  # older, paginated blocks
        absorb(json.loads(get(f"https://data.sec.gov/submissions/{extra['name']}")))

    rows.sort(key=lambda f: (f.period or "", f.filing_date), reverse=True)
    return rows


# ------------------------------------------------------------- 13F info tables

NS = re.compile(r"\{[^}]*\}")


def _tag(e) -> str:
    return NS.sub("", e.tag)


def _find(e, name: str):
    for c in e.iter():
        if _tag(c) == name:
            return c
    return None


def _text(e, name: str, default: str = "") -> str:
    c = _find(e, name)
    return (c.text or default).strip() if c is not None else default


@dataclass
class Holding:
    issuer: str
    cusip: str
    ticker: str = ""
    value_usd: float = 0.0          # 13F "value" column, in dollars
    shares: float = 0.0
    share_type: str = ""            # SH (shares) or PRN (principal)
    put_call: str = ""              # "", "Put", "Call"
    discretion: str = ""
    sole: float = 0.0
    shared: float = 0.0
    none_: float = 0.0


def fetch_13f_holdings(f: Filing) -> tuple[list[Holding], dict]:
    """Parse a 13F-HR's information table plus its cover-page summary."""
    idx = json.loads(get(f"{f.dir_url}/index.json"))
    names = [i["name"] for i in idx["directory"]["item"]]

    # Cover page / summary.  submissions.json prefixes primaryDocument with an
    # "xslForm13F_X02/" path that serves rendered HTML; strip it to get raw XML.
    meta: dict = {"accession": f.accession, "period": f.period, "filed": f.filing_date, "form": f.form}
    doc = f.primary_doc.split("/")[-1]
    if doc in names:
        try:
            root = ET.fromstring(get(f"{f.dir_url}/{doc}"))
            meta["manager"] = _text(root, "name")
            meta["report_type"] = _text(root, "reportType")
            meta["table_entry_total"] = _text(root, "tableEntryTotal")
            meta["table_value_total"] = _text(root, "tableValueTotal")
            meta["other_managers"] = _text(root, "otherIncludedManagersCount")
            meta["signed_by"] = _text(root, "signatureName") or _text(root, "name")
            meta["signature_title"] = _text(root, "title")
            meta["signature_date"] = _text(root, "signatureDate")
        except ET.ParseError:
            pass

    # Information table: the XML that isn't the primary doc and isn't the XSL stub
    cand = [
        n for n in names
        if n.lower().endswith(".xml") and "primary_doc" not in n.lower()
    ]
    holdings: list[Holding] = []
    for name in cand:
        try:
            root = ET.fromstring(get(f"{f.dir_url}/{name}"))
        except ET.ParseError:
            continue
        infos = [e for e in root.iter() if _tag(e) == "infoTable"]
        if not infos:
            continue
        for it in infos:
            sh = _find(it, "shrsOrPrnAmt")
            va = _find(it, "votingAuthority")
            holdings.append(
                Holding(
                    issuer=_text(it, "nameOfIssuer"),
                    cusip=_text(it, "cusip").upper(),
                    ticker=_text(it, "figi") and "" or "",
                    value_usd=_num(_text(it, "value")),
                    shares=_num(_text(sh, "sswpshPrnAmt") or _text(sh, "sshPrnamt")) if sh is not None else 0.0,
                    share_type=_text(sh, "sshPrnamtType") if sh is not None else "",
                    put_call=_text(it, "putCall"),
                    discretion=_text(it, "investmentDiscretion"),
                    sole=_num(_text(va, "Sole")) if va is not None else 0.0,
                    shared=_num(_text(va, "Shared")) if va is not None else 0.0,
                    none_=_num(_text(va, "None")) if va is not None else 0.0,
                )
            )
        meta["info_table"] = name
        break
    return holdings, meta


def _num(s: str) -> float:
    s = (s or "").replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def value_scale(holdings: list[Holding], meta: dict) -> float:
    """
    Pre-2023 13Fs reported value in thousands; post-2023 in whole dollars.
    Infer the multiplier by checking the sum against the cover page total
    (cover-page tableValueTotal is in dollars for modern filings).
    """
    total = sum(h.value_usd for h in holdings)
    cover = _num(meta.get("table_value_total", ""))
    if total and cover:
        ratio = cover / total
        if 900 < ratio < 1100:
            return 1000.0
    return 1.0


# --------------------------------------------------------------------- reports

def cmd_search(args):
    hits = search_filers(args.name)
    if not hits:
        print("no filers matched")
        return
    for h in hits:
        print(f"{h['cik']}  {h['name']}  {h['state']}")


def cmd_filings(args):
    forms = tuple(args.form) if args.form else None
    rows = list_filings(args.cik, forms)
    print(f"{'FORM':<12} {'PERIOD':<12} {'FILED':<12} ACCESSION")
    for f in rows[: args.limit]:
        print(f"{f.form:<12} {f.period or '-':<12} {f.filing_date:<12} {f.accession}")
    print(f"\n{len(rows)} filing(s) total")


def cmd_holdings(args):
    rows = list_filings(args.cik, ("13F-HR",))
    if args.period:
        rows = [r for r in rows if r.period == args.period]
    if not rows:
        print("no 13F-HR found for that CIK/period")
        return
    f = rows[0]
    holdings, meta = fetch_13f_holdings(f)
    scale = value_scale(holdings, meta)

    print(f"Manager : {meta.get('manager', '?')}")
    print(f"Form    : {meta.get('form')}  ({meta.get('report_type', '?')})")
    print(f"Period  : {meta.get('period')}   Filed: {meta.get('filed')}")
    print(f"Signed  : {meta.get('signed_by', '?')}, {meta.get('signature_title', '?')} on {meta.get('signature_date', '?')}")
    print(f"Cover   : {meta.get('table_entry_total', '?')} entries, ${_num(meta.get('table_value_total','0'))/1e6:,.0f}M")
    print(f"Source  : {f.index_url}\n")

    total = sum(h.value_usd for h in holdings) * scale
    print(f"{'ISSUER':<34} {'CUSIP':<10} {'P/C':<5} {'SHARES':>14} {'VALUE $M':>10} {'%':>6}")
    for h in sorted(holdings, key=lambda x: -x.value_usd):
        v = h.value_usd * scale
        print(
            f"{h.issuer[:33]:<34} {h.cusip:<10} {h.put_call or '-':<5} "
            f"{h.shares:>14,.0f} {v/1e6:>10,.1f} {100*v/total if total else 0:>5.1f}%"
        )
    print(f"\n{len(holdings)} positions, total ${total/1e6:,.0f}M")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(
                {"meta": meta, "scale": scale, "holdings": [asdict(h) for h in holdings]}, fh, indent=2
            )
        print(f"wrote {args.json}")


def cmd_sc13(args):
    rows = list_filings(args.cik, ("SC 13", "SC13", "SCHEDULE 13", "13D", "13G"))
    if not rows:
        print("no SC 13D/13G filings by this CIK")
        return
    for f in rows[: args.limit]:
        print(f"{f.form:<12} {f.filing_date:<12} {f.accession}  {f.index_url}")


def cmd_verify(args):
    """Compare a claimed position table (JSON) against the filed 13F."""
    claims = json.load(open(args.claims))
    rows = list_filings(args.cik, ("13F-HR",))
    if args.period:
        rows = [r for r in rows if r.period == args.period]
    if not rows:
        print("no matching 13F-HR — cannot verify")
        return
    holdings, meta = fetch_13f_holdings(rows[0])
    scale = value_scale(holdings, meta)

    # index filed holdings by (normalized issuer, put/call)
    def key(name: str, pc: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())[:12] + "|" + (pc or "").lower()

    filed = {}
    for h in holdings:
        filed.setdefault(key(h.issuer, h.put_call), []).append(h)

    print(f"Claimed table vs {meta.get('form')} period {meta.get('period')} filed {meta.get('filed')}\n")
    print(f"{'CLAIM':<28} {'TYPE':<6} {'CLAIM SH':>13} {'FILED SH':>13} {'CLAIM $M':>9} {'FILED $M':>9}  VERDICT")
    matched = 0
    for c in claims:
        k = key(c.get("issuer", c.get("ticker", "")), c.get("put_call", ""))
        hits = filed.get(k, [])
        if hits:
            matched += 1
            h = max(hits, key=lambda x: x.value_usd)
            v = h.value_usd * scale / 1e6
            sh_ok = abs(h.shares - c.get("shares", 0)) <= max(1, 0.01 * c.get("shares", 1))
            v_ok = abs(v - c.get("value_musd", 0)) <= max(1, 0.02 * c.get("value_musd", 1))
            verdict = "MATCH" if (sh_ok and v_ok) else ("shares ok, $ off" if sh_ok else "MISMATCH")
            print(
                f"{c.get('ticker', c.get('issuer', ''))[:27]:<28} {c.get('put_call') or 'EQ':<6} "
                f"{c.get('shares', 0):>13,.0f} {h.shares:>13,.0f} "
                f"{c.get('value_musd', 0):>9,.0f} {v:>9,.0f}  {verdict}"
            )
        else:
            print(
                f"{c.get('ticker', c.get('issuer', ''))[:27]:<28} {c.get('put_call') or 'EQ':<6} "
                f"{c.get('shares', 0):>13,.0f} {'—':>13} {c.get('value_musd', 0):>9,.0f} {'—':>9}  NOT IN FILING"
            )
    print(f"\n{matched}/{len(claims)} claimed lines found in the filing; filing has {len(holdings)} lines total")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search"); s.add_argument("name"); s.set_defaults(fn=cmd_search)
    s = sub.add_parser("filings"); s.add_argument("cik"); s.add_argument("--form", nargs="*")
    s.add_argument("--limit", type=int, default=50); s.set_defaults(fn=cmd_filings)
    s = sub.add_parser("holdings"); s.add_argument("cik"); s.add_argument("--period")
    s.add_argument("--json"); s.set_defaults(fn=cmd_holdings)
    s = sub.add_parser("sc13"); s.add_argument("cik"); s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_sc13)
    s = sub.add_parser("verify"); s.add_argument("claims"); s.add_argument("--cik", required=True)
    s.add_argument("--period"); s.set_defaults(fn=cmd_verify)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
