from datetime import date, datetime, timedelta, timezone

from .identity import parse_date


def verify(code, official, loaded=None, *, as_of=None, freshness_days=7, search_complete=False, errors=None):
    """Conservative evaluation: source observations, content completeness, applicability stay independent."""
    target = date.fromisoformat(as_of) if as_of else date.today()
    today = date.today()
    errors = errors or []
    base = [d for d in official if d["code"] == code and d.get("change_number") is None]
    amendments = [d for d in official if d["code"] == code and d.get("change_number") is not None]
    evidence = [
        {
            "snapshot_id": d["snapshot_id"],
            "provenance": d["provenance"],
            "fields": d["fields"],
            "change_number": d.get("change_number"),
        }
        for d in official
    ]
    unresolved, conflicts = [], []
    state = "unconfirmed"
    if not base:
        unresolved.append("No exact base document card confirmed in the official catalogue")
    elif len({(d["declared_status"], d.get("effective_from"), d.get("effective_to")) for d in base}) > 1:
        conflicts.append({"kind": "official_status_disagreement"})
    else:
        card = base[0]
        start, end = card.get("effective_from"), card.get("effective_to")
        observed = datetime.fromisoformat(card["provenance"]["checked_at"])
        fresh = datetime.now(timezone.utc) - observed <= timedelta(days=freshness_days)
        if start and target < date.fromisoformat(start):
            state = "not_yet_effective"
        elif end and target >= date.fromisoformat(end):
            state = (
                card["declared_status"]
                if card["declared_status"] in {"cancelled", "replaced"}
                else "cancelled"
            )
        elif target > today:
            unresolved.append("Future status cannot be certified from a current catalogue observation")
        elif target < today:
            unresolved.append("Current catalogue status alone does not establish historical status")
        elif not fresh:
            unresolved.append("Official status observation is older than the configured freshness limit")
        elif card["declared_status"] != "unknown":
            state = card["declared_status"]
        else:
            unresolved.append("Official card has no recognized status")
    if not search_complete:
        unresolved.append("Official search pagination or card retrieval is incomplete")
    required = sorted(
        {
            d["change_number"]
            for d in amendments
            if d.get("effective_from") and d["effective_from"] <= target.isoformat()
        }
    )
    if any(not d.get("effective_from") for d in amendments):
        unresolved.append("Some official amendments have no explicit effective date")
    content = "not_loaded"
    missing = []
    if loaded:
        if loaded["code"] != code or loaded.get("change_number") is not None:
            content = "identity_mismatch"
        elif loaded["text_status"] == "metadata_only":
            content = "metadata_only"
        else:
            included = loaded.get("included_changes", [])
            missing = sorted(set(required) - set(included))
            content = "outdated" if missing else "unconfirmed"
            future = sorted(
                {
                    d["change_number"]
                    for d in amendments
                    if d.get("effective_from") and d["effective_from"] > target.isoformat()
                }
                & set(included)
            )
            if future:
                content = "contains_future_changes"
            # Date of approval of an edition is not its effective date.
            if included and not missing and not future and search_complete and not errors:
                content = "amendments_accounted_for"
            # Cross-check secondary claims about amendment effective dates against the official card.
            import re

            for match in re.finditer(
                r"Изменение\s*(?:N|№)\s*(\d+).{0,240}?введен[оа]?\s+в\s+действие\s+с\s+"
                r"(\d{2}\.\d{2}\.\d{4})",
                loaded.get("text", ""),
                re.I,
            ):
                n, claimed = int(match[1]), parse_date(match[2])
                for amendment in amendments:
                    if amendment["change_number"] == n and amendment.get("effective_from"):
                        if claimed != amendment["effective_from"]:
                            conflicts.append(
                                {
                                    "kind": "effective_date_disagreement",
                                    "change_number": n,
                                    "official": amendment["effective_from"],
                                    "text_source": claimed,
                                    "source_url": loaded["provenance"]["source_url"],
                                    "official_url": amendment["provenance"]["source_url"],
                                }
                            )
    if conflicts:
        content = "conflict" if loaded else content
    if errors:
        unresolved.append("One or more live source requests failed; see source_errors")
    return {
        "code": code,
        "as_of": target.isoformat(),
        "territory": "RU",
        "document_status": state,
        "status_basis": "official_catalog_observation",
        "verification": "conflict"
        if conflicts
        else ("confirmed" if state != "unconfirmed" and not unresolved else "unconfirmed"),
        "text_revision_status": content,
        "required_changes": required,
        "missing_changes": missing,
        "applicability": "not_assessed",
        "evidence": evidence,
        "conflicts": conflicts,
        "unresolved": unresolved,
        "source_errors": errors,
        "limitations": [
            "Catalogue status does not certify mandatory application to a particular project.",
            "Amendment accounting is metadata reconciliation, not textual authentication.",
            "No complete amendment coverage is claimed beyond the returned official search.",
        ],
    }
