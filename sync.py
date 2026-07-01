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

# GHL pipeline config
PIPELINE_CONFIG = {
    "ATW Procurement": {
        "pipeline_id": "aWsAmf8I2r47X26Dcky5",
        "entry_stage_id": "4ed20b7e-9215-4dc2-b05f-42feec022d53",
    },
    "Infinity Grid Proposals": {
        "pipeline_id": "HXJQmVq4wpBffZVtATtP",
        "entry_stage_id": "1b0507e9-1476-42d9-b9dc-a95411cf194e",
    },
}

def get_sol_id(pursuit):
    """Extract the solicitation number from a HigherGov pursuit.

    Priority:
    1. Parse URL slug from highergov_opp_path
       e.g. /contract-opportunity/N6426726Q4103-Sources_Sought-55abe/
       -> N6426726Q4103
    2. version_number field (may contain sol number as string)
    3. Empty string
    """
    # 1. Parse from URL slug — this is the most reliable source
    opp_path = pursuit.get("highergov_opp_path") or ""
    match = re.search(r'/contract-opportunity/([^/]+)/', opp_path)
    if match:
        slug = match.group(1)
        parts = slug.split('-')
        # Sol number is before the first TitleCase word (Sources, Presolicitation, etc.)
        for i, part in enumerate(parts):
            if i > 0 and part and part[0].isupper():
                return '-'.join(parts[:i])
        return parts[0]

    # 2. version_number field (always cast to string)
    ver = pursuit.get("version_number")
    if ver is not None:
        ver_str = str(ver).strip()
        if ver_str and ver_str != "0":
            return ver_str

    return ""

def get_due_date(pursuit):
    """Return best available due date. Priority: proposal_due_date > source_soughts_due_date > solicitation_date"""
    for field in ("proposal_due_date", "source_soughts_due_date", "solicitation_date"):
        val = pursuit.get(field)
        if val:
            return str(val)[:10]
    return ""

def get_pursuits():
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

def backfill_sol_ids(pursuits, headers):
    """One-time backfill: update existing GHL opps that are missing a Solicitation ID.
    Matches by opportunity name, sets the sol ID custom field.
    Safe: only writes the sol ID field, never changes stage/status.
    """
    print("\nBackfilling Solicitation IDs on existing opps...")
    by_pipeline = defaultdict(list)
    for p in pursuits:
        by_pipeline[p.get("pipeline_name")].append(p)

    backfilled = 0
    for hgov_name, pipeline_pursuits in by_pipeline.items():
        config = PIPELINE_CONFIG[hgov_name]
        pipeline_id = config["pipeline_id"]

        # Fetch all existing opps in this pipeline
        page = 1
        while True:
            r = requests.get(
                "https://services.leadconnectorhq.com/opportunities/search",
                headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28"},
                params={"location_id": LOCATION_ID, "pipeline_id": pipeline_id, "limit": 100, "page": page},
            )
            data = r.json()
            opps = data.get("opportunities", [])
            if not opps:
                break

            for opp in opps:
                # Skip if already has a sol ID
                has_sol = any(
                    cf.get("id") == SOLICITATION_FIELD_ID and cf.get("fieldValue")
                    for cf in (opp.get("customFields") or [])
                )
                if has_sol:
                    continue

                opp_name = opp.get("name", "")
                # Match pursuit by name (strip [SOL_ID] prefix if present)
                name_bare = re.sub(r'^\[[^\]]+\]\s*', '', opp_name)
                for p in pipeline_pursuits:
                    sol_id = get_sol_id(p)
                    pursuit_name = p.get("pursuit_name") or ""
                    if not sol_id:
                        continue
                    if pursuit_name == name_bare or opp_name == f"[{sol_id}] {pursuit_name}" or pursuit_name in opp_name:
                        # Update only the custom fields — never touch stage/status
                        patch = requests.put(
                            f"https://services.leadconnectorhq.com/opportunities/{opp['id']}",
                            headers=headers,
                            json={"customFields": [{"id": SOLICITATION_FIELD_ID, "field_value": sol_id}]},
                        )
                        if patch.status_code in (200, 201):
                            backfilled += 1
                            print(f"  BACKFILL: [{sol_id}] {pursuit_name}")
                        break

            if not data.get("meta", {}).get("nextPageUrl"):
                break
            page += 1

    print(f"Backfill complete: {backfilled} opps updated with Solicitation ID")

def sync():
    pursuits = get_pursuits()
    headers = {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }
    created = skipped = errors = 0

    # Backfill sol IDs on existing opps that are missing them
    backfill_sol_ids(pursuits, headers)

    by_pipeline = defaultdict(list)
    for p in pursuits:
        by_pipeline[p.get("pipeline_name")].append(p)

    for hgov_pipeline_name, pipeline_pursuits in by_pipeline.items():
        config = PIPELINE_CONFIG[hgov_pipeline_name]
        pipeline_id = config["pipeline_id"]
        entry_stage_id = config["entry_stage_id"]

        existing_sol_ids, existing_names = get_existing_opps(pipeline_id)
        print(
            f"\nPipeline '{hgov_pipeline_name}': {len(pipeline_pursuits)} in HGov, "
            f"{len(existing_names)} already in GHL"
        )

        for p in pipeline_pursuits:
            pursuit_name = p.get("pursuit_name") or "Unnamed"
            sol_id = get_sol_id(p)
            opp_name = f"[{sol_id}] {pursuit_name}" if sol_id else pursuit_name

            if sol_id and sol_id in existing_sol_ids:
                skipped += 1
                continue
            if opp_name in existing_names or pursuit_name in existing_names:
                skipped += 1
                continue

            value = float(p.get("est_value") or p.get("weighted_value") or 0)
            due_date = get_due_date(p)

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
                "pipelineStageId": entry_stage_id,
                "status": "open",
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
        f"\nSync complete: {created} created, {skipped} skipped, {errors} errors"
    )

if __name__ == "__main__":
    sync()
