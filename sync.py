import os
import re
import requests
from collections import defaultdict

HGOV_KEY = os.environ["HIGHERGOV_API_KEY"]
GHL_TOKEN = os.environ["GHL_API_KEY"]
LOCATION_ID = "nN5rGX4FAVMJFzv4Qvdy"

# GHL custom field ID for Solicitation ID
SOLICITATION_FIELD_ID = "ttYlo5NmQ5HZhyLckDRO"

# GHL pipeline config — one entry per HigherGov pipeline name
PIPELINE_CONFIG = {
    "ATW Procurement": {
        "pipeline_id": "aWsAmf8I2r47X26Dcky5",
        "stages": {
            "New Lead":      "4ed20b7e-9215-4dc2-b05f-42feec022d53",
            "Qualified":     "b8290a41-17f5-47dd-a272-79d9f6411f86",
            "Proposal Sent": "0347223d-c71c-4f21-8f2c-82112dfc8593",
            "Negotiation":   "e9f45a45-ac29-4192-a7ea-15b5de48fe71",
            "Closed":        "b4c158e0-da09-4a2e-a3a6-a2a39038187f",
        }
    },
    "Infinity Grid Proposals": {
        "pipeline_id": "HXJQmVq4wpBffZVtATtP",
        "stages": {
            "New Lead":      "1b0507e9-1476-42d9-b9dc-a95411cf194e",
            "Qualified":     "c33df125-870c-4ae4-b3e0-68e7178575a8",
            "Proposal Sent": "12d5af56-7c77-4c4a-8a1d-877e0c52218b",
            "Negotiation":   "cdeae4fb-9607-4bd1-b7be-b7973d8d5208",
            "Closed":        "2a626c6c-28f8-4655-8474-57d4b0b46685",
        }
    }
}

def extract_solicitation_id(opp_path):
    """Extract solicitation number from HigherGov opportunity URL.
    e.g. https://www.highergov.com/contract-opportunity/N6426726Q4103-Sources_Sought-55abe/
    returns 'N6426726Q4103'
    """
    if not opp_path:
        return ""
    match = re.search(r'/contract-opportunity/([^/]+)/', opp_path)
    if match:
        slug = match.group(1)
        # The solicitation ID is everything before the first hyphen+type segment
        # Pattern: SOLICIT_ID-Type_Description-hash
        parts = slug.split('-')
        # Find where the type description starts (capitalized word like Sources, Solicitation, etc.)
        for i, part in enumerate(parts):
            if i > 0 and part[0].isupper():
                return '-'.join(parts[:i])
        return parts[0]
    return ""

def map_stage(s):
    s = (s or "").lower()
    if any(x in s for x in ["closed", "no bid", "lost", "award"]): return "Closed"
    if any(x in s for x in ["proposal submitted", "proposal/pricing", "prebid", "compliance"]): return "Proposal Sent"
    if any(x in s for x in ["qualifying", "sourcing", "emailed", "atw"]): return "Qualified"
    return "New Lead"

def get_pursuits():
    items, page = [], 1
    while True:
        r = requests.get("https://www.highergov.com/api-external/pursuit/",
            params={"api_key": HGOV_KEY, "page_size": 100, "page_number": page})
        data = r.json()
        items += data.get("results", [])
        if not data.get("next"): break
        page += 1
    filtered = [p for p in items if p.get("pipeline_name") in PIPELINE_CONFIG]
    print(f"Fetched {len(items)} total pursuits, {len(filtered)} from target pipelines")
    return filtered

def get_existing_opps(pipeline_id):
    """Return dict keyed by solicitation_id custom field value (falls back to name)."""
    r = requests.get("https://services.leadconnectorhq.com/opportunities/search",
        headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28"},
        params={"location_id": LOCATION_ID, "pipeline_id": pipeline_id, "limit": 100})
    opps = r.json().get("opportunities", [])
    by_sol_id = {}
    by_name = {}
    for o in opps:
        # Index by solicitation ID if present
        for cf in (o.get("customFields") or []):
            if cf.get("id") == SOLICITATION_FIELD_ID and cf.get("fieldValue"):
                by_sol_id[cf["fieldValue"]] = o
        by_name[o["name"]] = o
    return by_sol_id, by_name

def create_contact(name):
    r = requests.post("https://services.leadconnectorhq.com/contacts/",
        headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28", "Content-Type": "application/json"},
        json={"firstName": name[:40], "lastName": "[HGov]", "locationId": LOCATION_ID, "tags": ["highergov"]})
    return r.json().get("contact", {}).get("id")

def sync():
    pursuits = get_pursuits()
    headers = {"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28", "Content-Type": "application/json"}
    created = updated = errors = 0

    by_pipeline = defaultdict(list)
    for p in pursuits:
        by_pipeline[p.get("pipeline_name")].append(p)

    for hgov_name, pipeline_pursuits in by_pipeline.items():
        config = PIPELINE_CONFIG[hgov_name]
        pipeline_id = config["pipeline_id"]
        stage_ids = config["stages"]
        by_sol_id, by_name = get_existing_opps(pipeline_id)
        print(f"\nSyncing '{hgov_name}' -> GHL pipeline {pipeline_id}")
        print(f"  {len(pipeline_pursuits)} pursuits from HGov, {len(by_name)} already in GHL")

        for p in pipeline_pursuits:
            pursuit_name = p.get("pursuit_name") or "Unnamed"
            sol_id = extract_solicitation_id(p.get("highergov_opp_path", ""))
            # Opportunity name includes solicitation ID as prefix for pipeline card visibility
            opp_name = f"[{sol_id}] {pursuit_name}" if sol_id else pursuit_name

            stage = map_stage(p.get("stage_name", ""))
            stage_id = stage_ids[stage]
            status = "won" if stage == "Closed" else "open"
            value = float(p.get("est_value") or p.get("weighted_value") or 0)

            custom_fields = [{"id": SOLICITATION_FIELD_ID, "field_value": sol_id}] if sol_id else []

            payload = {
                "name": opp_name,
                "pipelineId": pipeline_id,
                "pipelineStageId": stage_id,
                "status": status,
                "monetaryValue": value,
                "customFields": custom_fields,
            }

            # Match by solicitation ID first (most reliable), then by name
            existing = by_sol_id.get(sol_id) or by_name.get(opp_name) or by_name.get(pursuit_name)

            if existing:
                r = requests.put(f"https://services.leadconnectorhq.com/opportunities/{existing['id']}",
                    headers=headers, json=payload)
                resp = r.json()
                if resp.get("opportunity"):
                    updated += 1
                    print(f"  UPDATED: [{sol_id}] {pursuit_name}")
                else:
                    errors += 1
                    print(f"  UPDATE ERR [{sol_id}] {pursuit_name}: {r.text[:120]}")
            else:
                cid = create_contact(opp_name)
                if not cid: errors += 1; continue
                payload["contactId"] = cid
                r = requests.post("https://services.leadconnectorhq.com/opportunities/",
                    headers=headers, json=payload)
                resp = r.json()
                if resp.get("id"):
                    created += 1
                    print(f"  CREATED: [{sol_id}] {pursuit_name}")
                else:
                    errors += 1
                    print(f"  CREATE ERR [{sol_id}] {pursuit_name}: {r.text[:120]}")

    print(f"\nSync complete: {created} created, {updated} updated, {errors} errors (of {len(pursuits)} total)")

if __name__ == "__main__":
    sync()
