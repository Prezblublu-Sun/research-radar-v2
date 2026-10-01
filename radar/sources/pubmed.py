"""PubMed fetcher: uses NCBI E-utilities (esearch + efetch) to find recent
papers matching MeSH terms or keywords. Free, no API key (rate limited to 3 req/s
without key, 10/s with key). Returns abstracts when available."""

from __future__ import annotations
import os
import datetime as dt
import xml.etree.ElementTree as ET
import time
import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EMAIL = os.environ.get("PUBMED_EMAIL", "your-email@ucl.ac.uk")
API_KEY = os.environ.get("PUBMED_API_KEY", "")


def _common_params() -> dict:
    p = {"tool": "research-radar", "email": EMAIL}
    if API_KEY:
        p["api_key"] = API_KEY
    return p


def _build_query(terms: list[str], days_back: int,
                 from_date: str | None = None,
                 to_date: str | None = None) -> str:
    if not terms:
        return ""
    or_clause = " OR ".join(f'"{t}"[Title/Abstract]' for t in terms)
    if from_date and to_date:
        f = from_date.replace("-", "/")
        t = to_date.replace("-", "/")
        return f'({or_clause}) AND ("{f}"[EDAT] : "{t}"[EDAT])'
    return f"({or_clause}) AND \"last {days_back} days\"[EDAT]"


def fetch(terms: list[str], days_back: int = 1,
          retmax: int | None = None,
          from_date: str | None = None,
          to_date: str | None = None) -> list[dict]:
    """Returns normalized PubMed papers.

    Two modes:
      - Daily (default): `"last N days"[EDAT]` query, default `retmax`=200.
      - Historical (when both from_date and to_date are set as 'YYYY-MM-DD'):
        `"YYYY/MM/DD"[EDAT] : "YYYY/MM/DD"[EDAT]` range query, default
        `retmax` bumped to 500 internally.

    Callers may always override `retmax` explicitly.
    """
    if not terms:
        return []

    historical = bool(from_date and to_date)
    if historical:
        effective_retmax = retmax if retmax is not None else 500
    else:
        effective_retmax = retmax if retmax is not None else 200

    query = _build_query(terms, days_back, from_date=from_date, to_date=to_date)

    r = requests.get(
        f"{EUTILS}/esearch.fcgi",
        params={**_common_params(), "db": "pubmed", "term": query,
                "retmax": effective_retmax, "retmode": "json"},
        timeout=30,
    )
    r.raise_for_status()
    pmids = r.json().get("esearchresult", {}).get("idlist", [])
    if not pmids:
        return []

    out: list[dict] = []
    for i in range(0, len(pmids), 100):
        batch = pmids[i:i+100]
        rr = requests.get(
            f"{EUTILS}/efetch.fcgi",
            params={**_common_params(), "db": "pubmed",
                    "id": ",".join(batch), "retmode": "xml"},
            timeout=60,
        )
        rr.raise_for_status()
        out.extend(_parse_efetch(rr.text))
        time.sleep(0.4 if not API_KEY else 0.1)
    return out


def _parse_efetch(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    papers = []
    for art in root.findall(".//PubmedArticle"):
        try:
            papers.append(_extract_one(art))
        except Exception as e:
            import traceback
            print(f"  ! _extract_one failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue
    return papers


def _extract_one(art) -> dict:
    pmid = art.findtext(".//PMID") or ""
    title = art.findtext(".//ArticleTitle") or ""

    abstract_parts = []
    for ab in art.findall(".//Abstract/AbstractText"):
        label = ab.attrib.get("Label", "")
        text = "".join(ab.itertext()).strip()
        abstract_parts.append(f"{label}: {text}" if label else text)
    abstract = " ".join(abstract_parts)

    authors = []
    author_list = []
    for au in art.findall(".//Author"):
        fn = au.findtext("ForeName") or ""
        ln = au.findtext("LastName") or ""
        name = f"{fn} {ln}".strip()
        if not name:
            continue
        authors.append(name)
        affs = [a.findtext("Affiliation") or "" for a in au.findall("AffiliationInfo")]
        affs = [a for a in affs if a]
        author_list.append({
            "name": name,
            "affiliation": "; ".join(affs) if affs else "",
        })

    first_author_affiliation = author_list[0]["affiliation"] if author_list else ""

    corresponding = []
    for a in author_list:
        aff_lower = a["affiliation"].lower()
        if "corresponding author" in aff_lower or "@" in a["affiliation"]:
            corresponding.append(a)
    if not corresponding and len(author_list) > 1:
        last = author_list[-1]
        if last["affiliation"]:
            corresponding = [{"name": last["name"], "affiliation": last["affiliation"], "inferred": True}]

    venue = art.findtext(".//Journal/Title") or ""
    year = art.findtext(".//PubDate/Year") or ""
    month = art.findtext(".//PubDate/Month") or "01"
    day = art.findtext(".//PubDate/Day") or "01"

    doi = ""
    for aid in art.findall(".//ArticleId"):
        if aid.attrib.get("IdType") == "doi":
            doi = (aid.text or "").strip()
            break

    try:
        month_num = month if month.isdigit() else dt.datetime.strptime(month[:3], "%b").month
        date = f"{year}-{int(month_num):02d}-{int(day):02d}" if year else ""
    except Exception:
        date = f"{year}-01-01" if year else ""

    return {
        "source": "pubmed",
        "id": f"pubmed:{pmid}",
        "doi": doi,
        "pmid": pmid,
        "title": title.strip(),
        "abstract": abstract,
        "authors": authors,
        "first_author_affiliation": first_author_affiliation,
        "corresponding_authors": corresponding,
        "venue": venue,
        "year": int(year) if year.isdigit() else None,
        "date": date,
        # PubMed uses EDAT (Entrez Date — when the record entered PubMed); always day-precision per ADR-0015 §4.1
        "date_precision": "day",
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "cited_by_count": 0,
        "concepts": [],
        "categories": [],
        "raw_type": "journal article",
    }


if __name__ == "__main__":
    sample = fetch(["femoral stem", "stress shielding"], days_back=30, retmax=10)
    print(f"Fetched {len(sample)} papers")
    for p in sample[:3]:
        print(f"  - {p['title'][:80]}  [PMID:{p['pmid']}]")
