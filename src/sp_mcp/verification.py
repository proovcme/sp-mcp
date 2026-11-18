import re
from datetime import date, datetime, timedelta, timezone

from .identity import parse_date


def today_moscow():
    return datetime.now(timezone(timedelta(hours=3))).date()


def verify(code, official, loaded=None, *, as_of=None, freshness_days=7, search_complete=False, errors=None):
    """Conservative evaluation: source observations, content completeness, applicability stay independent."""
    today = today_moscow()
    target = date.fromisoformat(as_of) if as_of else today
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
    supersedes = sorted({code for card in base for code in card.get("supersedes", [])})
    superseded_by = sorted({code for card in base for code in card.get("superseded_by", [])})
    other_changes = [change for card in base for change in card.get("other_change_links", [])]
    if target != today:
        unresolved.append("Current catalogue observations do not certify status on another date")
    if not base:
        unresolved.append("No exact base document card confirmed in the official catalogue")
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
        recent = [card for card in base if datetime.fromisoformat(card["provenance"]["checked_at"]) >= cutoff]
        known = {}
        for key in ("declared_status", "effective_from", "effective_to"):
            values = {d[key] for d in (recent or base) if d.get(key) not in {None, "", "unknown"}}
            if len(values) > 1:
                conflicts.append(
                    {"kind": "official_status_disagreement", "field": key, "values": sorted(values)}
                )
            known[key] = next(iter(values)) if len(values) == 1 else None
        start, end = known["effective_from"], known["effective_to"]
        declared_status = known["declared_status"]
        if not recent:
            unresolved.append("Official status observation is older than the configured freshness limit")
        if conflicts:
            pass
        elif start and end and start >= end:
            conflicts.append({"kind": "invalid_base_effective_period"})
        elif target == today and (
            (end and end > target.isoformat() and declared_status in {"cancelled", "replaced"})
            or (end and end <= target.isoformat() and declared_status == "active")
            or (start and start > target.isoformat() and declared_status == "active")
        ):
            conflicts.append({"kind": "official_status_effective_date_disagreement"})
        elif start and target < date.fromisoformat(start):
            state = "not_yet_effective"
        elif end and target >= date.fromisoformat(end):
            state = declared_status if declared_status in {"cancelled", "replaced"} else "cancelled"
        elif target == today and recent:
            if declared_status:
                state = declared_status
            else:
                unresolved.append("Official card has no recognized status")
    if not search_complete:
        unresolved.append("Official search pagination or card retrieval is incomplete")
    amendment_dates = {}
    for amendment in amendments:
        amendment_dates.setdefault(amendment["change_number"], set()).add(amendment.get("effective_from"))
    undated = sorted(number for number, dates in amendment_dates.items() if not dates - {None})
    for number, dates in amendment_dates.items():
        if len(dates - {None}) > 1:
            conflicts.append({"kind": "official_amendment_date_disagreement", "change_number": number})
    required = sorted(
        {
            d["change_number"]
            for d in amendments
            if d.get("effective_from") and d["effective_from"] <= target.isoformat()
        }
    )
    if undated:
        unresolved.append("Some official amendments have no explicit effective date")
    content = "not_loaded"
    missing = []
    unknown = []
    if loaded:
        if loaded["code"] != code or loaded.get("change_number") is not None:
            content = "identity_mismatch"
        elif loaded["text_status"] == "metadata_only":
            content = "metadata_only"
        elif loaded["text_status"] == "extracted_pdf" and not loaded.get("identity_verified_from_cover"):
            content = "identity_unconfirmed"
        else:
            included = set(loaded.get("included_changes", []))
            missing = sorted(set(required) - included)
            unknown = sorted(included - amendment_dates.keys())
            content = "outdated" if missing else "unconfirmed"
            future = sorted(
                {
                    d["change_number"]
                    for d in amendments
                    if d.get("effective_from") and d["effective_from"] > target.isoformat()
                }
                & included
            )
            if future:
                content = "contains_future_changes"
            # Date of approval of an edition is not its effective date.
            if (
                required
                and not missing
                and not future
                and not unknown
                and not undated
                and not other_changes
                and not unresolved
                and state != "unconfirmed"
                and not errors
            ):
                content = "amendments_accounted_for"
            # Cross-check secondary claims about amendment effective dates against the official card.
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
        "verification_rules": 2,
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
        "unknown_changes": unknown,
        "undated_changes": undated,
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "other_changes": other_changes,
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
