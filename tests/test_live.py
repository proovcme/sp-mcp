"""Small opt-in network acceptance check; no source texts or private results are committed."""

import pytest

from sp_mcp.service import Service


@pytest.mark.live
def test_live_catalogue_edition_and_official_status(config):
    service = Service(config)
    result = service.search("СП 60.13330.2020", refresh=True)
    assert not result["source_errors"]
    items = [item for item in result["items"] if item["code"] == "СП 60.13330.2020"]
    secondary = next(item for item in items if item["source"] == "meganorm")
    opened = service.open(secondary["url"], latest_revision=True)
    assert opened["text_length"] > 10000
    assert service.read_clause(opened["snapshot_id"], "7.11.1")["status"] == "found"
    report = service.check("СП 60.13330.2020", opened["snapshot_id"], refresh=True)
    assert report["evidence"]
    assert report["verification"] in {"confirmed", "conflict", "unconfirmed"}
    assert report["applicability"] == "not_assessed"
    assert all(e["provenance"]["authority"] == "official_catalog" for e in report["evidence"])
