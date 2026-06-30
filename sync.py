import os
import re
import requests
from collections import defaultdict

HGOV_KEY = os.environ["HIGHERGOV_API_KEY"]
GHL_TOKEN = os.environ["GHL_API_KEY"]
LOCATION_ID = "nN5rGX4FAVMJFzv4Qvdy"

# GHL custom field IDs for opportunities
SOLICITATION_FIELD_ID = "ttYlo5NmQ5HZhyLckDRO"
DUE_DATE_FIELD_ID = "bpholONMpvxCF2arQ8jJ"

# GHL pipeline config — only the entry stage ID is needed per pipeline.
# New opportunities always land in "Added to Pipeline".
# Existing opportunities are NEVER touched — users own stage movement in GHL.
PIPELINE_CONFIG = {
    "ATW Procurement": {
        "pipeline_id": "aWsAmf8I2r47X26Dcky5",
        "entry_stage_id": "4ed20b7e-9215-4dc2-b05f-42feec022d53",  # Added to Pipeline
    },
    "Infinity Grid Proposals": {
        "pipeline_id": "HXJQmVq4wpBffZVtATtP",
        "entry_stage_id": "1b0507e9-1476-42d9-b9dc-a95411cf194e",  # Pre-Pipeline
    },
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
        parts = slug.split('-')
        for i, part in enumerate(parts):
            if i > 0 and part[0].isupper():
                return '-'.join(parts[:i])
        return parts[0]
    return ""


def get_due_date(pursuit):
    """Return the best available due date from a HigherGov pursuit record.
    Priority: proposal_due_date > source_soughts_due_date > solicitation_date
    Returns a string like '2025-09-15' or '' if none available.
    """
    for field in ("proposal_due_date", "source_soughts_due_date", "solicitation_date"):
        val = pursuit.get(field)
        if val:
            # HigherGov returns dates as ISO strings (YYYY-MM-DD or YYYY-MM-DDThh:mm:ssZ)
            return str(val)[:10]
    return ""


def get_pursuits():
    """Fetch all pursuits from HigherGov that belong to a tracked pipeline."""
    items, page = [], 1
    while True:
        r = requests.get(
            "https://www.highergov.com/api-external/pursuit/",
            params={"api_key": HGOV_KEY, "page_size": 100, "page_number": page},
        )
        data = r.json()
        items += data.get("results", [])
        if not data.get("next"):
            break
        page += 1
    filtered = [p for p in items if p.get("pipeline_name") in PIPELINE_CONFIG]
    print(f"Fetched {len(items)} total pursuits, {len(filtered)} from tracked pipelines")
    return filtered


def get_existing_opps(pipeline_id):
    """Return sets of solicitation IDs and opportunity names already in this GHL pipeline.
    Used only to detect duplicates — we never update existing opps.
    """
    sol_ids, names = set(), set()
    r = requests.get(
        "https://services.leadconnectorhq.com/opportunities/search",
        headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28"},
        params={"location_id": LOCATION_ID, "pipeline_id": pipeline_id, "limit": 100},
    )
    for o in r.json().get("opportunities", []):
        names.add(o["name"])
        for cf in (o.get("customFields") or []):
            if cf.get("id") == SOLICITATION_FIELD_ID and cf.get("fieldValue"):
                sol_ids.add(cf["fieldValue"])
    return sol_ids, names


def search_contact_by_name(first_name):
    """Search GHL contacts by firstName to find an existing placeholder contact."""
    r = requests.get(
        "https://services.leadconnectorhq.com/contacts/",
        headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28"},
        params={"locationId": LOCATION_ID, "query": first_name[:40], "limit": 5},
    )
    for c in r.json().get("contacts", []):
        if c.get("firstName", "")[:40] == first_name[:40] and c.get("lastName") == "[HGov]":
            return c.get("id")
    return None


def get_or_create_contact(name):
    """Return an existing placeholder contact ID, or create a new one."""
    existing_id = search_contact_by_name(name)
    if existing_id:
        return existing_id
    r = requests.post(
        "https://services.leadconnectorhq.com/contacts/",
        headers={
            "Authorization": f"Bearer {GHL_TOKEN}",
            "Version": "2021-07-28",
            "Content-Type": "application/json",
        },
        json={
            "firstName": name[:40],
            "lastName": "[HGov]",
            "locationId": LOCATION_ID,
            "tags": ["highergov"],
        },
    )
    return r.json().get("contact", {}).get("id")


def sync():
    pursuits = get_pursuits()
    headers = {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }
    created = skipped = errors = 0

    by_pipeline = defaultdict(list)
    for p in pursuits:
        by_pipeline[p.get("pipeline_name")].append(p)

    for hgov_pipeline_name, pipeline_pursuits in by_pipeline.items():
        config = PIPELINE_CONFIG[hgov_pipeline_name]
        pipeline_id = config["pipeline_id"]
        entry_stage_id = config["entry_stage_id"]

        # Fetch existing opps — only used to skip duplicates, never to update
        existing_sol_ids, existing_names = get_existing_opps(pipeline_id)
        print(
            f"\nPipeline '{hgov_pipeline_name}': {len(pipeline_pursuits)} in HGov, "
            f"{len(existing_names)} already in GHL"
        )

        for p in pipeline_pursuits:
            pursuit_name = p.get("pursuit_name") or "Unnamed"
            sol_id = extract_solicitation_id(p.get("highergov_opp_path", ""))
            opp_name = f"[{sol_id}] {pursuit_name}" if sol_id else pursuit_name

            # SKIP if already in GHL — users own stage/status, we never overwrite
            if sol_id and sol_id in existing_sol_ids:
                skipped += 1
                print(f"  SKIP (exists): [{sol_id}] {pursuit_name}")
                continue
            if opp_name in existing_names or pursuit_name in existing_names:
                skipped += 1
                print(f"  SKIP (exists): {pursuit_name}")
                continue

            # NEW opportunity — create it in "Added to Pipeline", status open
            value = float(p.get("est_value") or p.get("weighted_value") or 0)
            due_date = get_due_date(p)

            # Build custom fields list: always include Solicitation ID; add Due Date if available
            custom_fields = []
            if sol_id:
                custom_fields.append({"id": SOLICITATION_FIELD_ID, "field_value": sol_id})
            if due_date:
                custom_fields.append({"id": DUE_DATE_FIELD_ID, "field_value": due_date})

            cid = get_or_create_contact(opp_name)
            if not cid:
                errors += 1
                print(f"  ERROR: could not get/create contact for {pursuit_name}")
                continue

            payload = {
                "name": opp_name,
                "pipelineId": pipeline_id,
                "pipelineStageId": entry_stage_id,  # Always "Added to Pipeline"
                "status": "open",                    # Always open on creation
                "monetaryValue": value,
                "contactId": cid,
                "customFields": custom_fields,
            }

            r = requests.post(
                "https://services.leadconnectorhq.com/opportunities/",
                headers=headers,
                json=payload,
            )
            resp = r.json()
            if resp.get("id"):
                created += 1
                due_str = f" (due {due_date})" if due_date else ""
                print(f"  CREATED: [{sol_id}] {pursuit_name} -> Added to Pipeline{due_str}")
            else:
                errors += 1
                print(f"  CREATE ERR [{sol_id}] {pursuit_name}: {r.text[:120]}")

    print(
        f"\nSync complete: {created} created, {skipped} skipped (already in GHL), "
        f"{errors} errors (of {len(pursuits)} total)"
    )


if __name__ == "__main__":
    sync()
