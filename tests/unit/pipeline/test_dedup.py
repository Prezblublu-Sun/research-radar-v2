"""Pure dedup stage (ported from v1 aggregator tests, minus the file I/O)."""
from __future__ import annotations

from radar.pipeline import dedup


def test_openalex_copy_of_arxiv_preprint_is_one_paper_and_arxiv_wins(paper_factory):
    arxiv = paper_factory("arxiv", arxiv_id="2609.00965v1")
    copy = paper_factory("openalex", arxiv_id="2609.00965", abstract="", oa_id="https://openalex.org/W7207880362")
    result = dedup.dedup_stage([[arxiv], [copy]], seen=set())
    assert len(result.papers) == 1
    assert result.papers[0]["source"] == "arxiv"
    assert result.papers[0]["also_seen_in"] == "openalex"
    assert result.new_keys == {"arxiv:2609.00965"}
    assert result.merged == 1


def test_published_version_with_doi_supersedes_arxiv_record(paper_factory):
    arxiv = paper_factory("arxiv", doi="10.1038/s41524-026-02226-3", arxiv_id="2601.06820v1")
    journal = paper_factory("openalex", doi="10.1038/S41524-026-02226-3", oa_id="https://openalex.org/W1")
    result = dedup.dedup_stage([[arxiv], [journal]], seen=set())
    assert len(result.papers) == 1
    assert result.papers[0]["source"] == "openalex"
    assert result.papers[0]["also_seen_in"] == "arxiv"
    assert result.new_keys == {"doi:10.1038/s41524-026-02226-3"}


def test_richer_abstract_wins_regardless_of_order(paper_factory):
    empty = paper_factory("openalex", doi="10.1/x", abstract="", oa_id="https://openalex.org/W1")
    full = paper_factory("pubmed", doi="10.1/x", pmid="1", abstract="A full abstract.")
    result = dedup.dedup_stage([[empty], [full]], seen=set())
    assert result.papers[0]["abstract"] == "A full abstract."


def test_zenodo_pair_collapses_with_aliases_but_not_without(paper_factory):
    aliases = {"10.5281/zenodo.22057604": "10.5281/zenodo.22057603"}
    concept = paper_factory("openalex", doi="10.5281/zenodo.22057603", oa_id="https://openalex.org/W1")
    version = paper_factory("openalex", doi="10.5281/zenodo.22057604", oa_id="https://openalex.org/W2")
    assert len(dedup.dedup_stage([[concept, version]], set(), aliases).papers) == 1
    assert len(dedup.dedup_stage([[concept, version]], set()).papers) == 2


def test_pubmed_and_openalex_join_on_pmid_when_doi_is_missing(paper_factory):
    pm = paper_factory("pubmed", pmid="42709028")
    oa = paper_factory("openalex", pmid="42709028", oa_id="https://openalex.org/W9", abstract="")
    result = dedup.dedup_stage([[pm], [oa]], set())
    assert len(result.papers) == 1 and result.new_keys == {"pmid:42709028"}


def test_seen_keys_suppress_refetch_unless_forced(paper_factory):
    seen = {"doi:10.1/old"}
    old = paper_factory("openalex", doi="10.1/OLD", oa_id="https://openalex.org/W3")
    fresh = paper_factory("arxiv", arxiv_id="2601.00002v1")
    result = dedup.dedup_stage([[old, fresh]], seen)
    assert [p["source"] for p in result.papers] == ["arxiv"]
    assert result.already_seen == 1
    forced = dedup.dedup_stage([[old, fresh]], seen, force=True)
    assert len(forced.papers) == 2 and forced.already_seen == 0


def test_identity_less_records_fall_back_to_source_id(paper_factory):
    a = paper_factory("pubmed", title="No identifiers at all"); a["id"] = "pubmed:"
    b = paper_factory("pubmed", title="No identifiers at all"); b["id"] = "pubmed:"
    assert len(dedup.dedup_stage([[a], [b]], set()).papers) == 1
    assert dedup.dedup_key(a) == "pubmed:pubmed:"
