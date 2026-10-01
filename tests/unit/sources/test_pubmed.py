"""PubMed fetcher: EDAT gives day precision (ported)."""
from __future__ import annotations

import re

from radar.sources import pubmed as pubmed_fetcher

_FAKE_EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <Journal><Title>Journal of Test</Title></Journal>
        <ArticleTitle>A test article about femoral stems</ArticleTitle>
        <Abstract><AbstractText>Background test content.</AbstractText></Abstract>
        <AuthorList>
          <Author>
            <LastName>Smith</LastName><ForeName>Jane</ForeName>
            <AffiliationInfo><Affiliation>UCL</Affiliation></AffiliationInfo>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <History><PubDate><Year>2024</Year><Month>03</Month><Day>15</Day></PubDate></History>
      <ArticleIdList><ArticleId IdType="doi">10.1/test</ArticleId></ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def _install(monkeypatch):
    class FakeResp:
        def __init__(self, *, json_data=None, text=""):
            self._json, self.text = json_data, text
        def raise_for_status(self): pass
        def json(self): return self._json

    def fake_get(url, params=None, **kw):
        if "esearch" in url:
            return FakeResp(json_data={"esearchresult": {"idlist": ["12345678"]}})
        if "efetch" in url:
            return FakeResp(text=_FAKE_EFETCH_XML)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(pubmed_fetcher.requests, "get", fake_get)
    monkeypatch.setattr(pubmed_fetcher.time, "sleep", lambda *a, **kw: None)


def test_paper_has_day_precision_iso_date_and_pmid(monkeypatch):
    _install(monkeypatch)
    papers = pubmed_fetcher.fetch(["femoral stem"], days_back=7)
    assert len(papers) == 1
    assert papers[0]["date_precision"] == "day"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", papers[0]["date"])
    assert papers[0]["date"] == "2024-03-15"
    assert papers[0]["pmid"] == "12345678"
    assert papers[0]["doi"] == "10.1/test"
