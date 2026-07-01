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

GHL_HEADERS = {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
}


def get_sol_id(pursuit):
        """Extract solicitation number from a HigherGov pursuit.

            Strategy (in priority order):
                1. reference_id field (direct field, most reliable for all types)
                    2. Parse URL slug from highergov_opp_path
                        3. version_number field as fallback
                            """
        # 1. reference_id — direct field, handles NSN/supply/RFQ types
        ref = pursuit.get("reference_id")
        if ref is not None:
                    ref_str = str(ref).strip()
                    if ref_str and ref_str not in ("0", "None", ""):
                                    return ref_str

                # 2. Parse from URL slug
                opp_path = pursuit.get("highergov_opp_path") or ""
    match = re.search(r'/contract-opportunity/([^/?#]+)', opp_path)
    if match:
                slug = match.group(1)
                # Sol number is before the first TitleCase word (Sources, Presolicitation, etc.)
                # Pattern: <SOL-ID>-TitleCaseWord-...
                # Also handle all-caps slug sections
                parts = slug.split('-')
                for i, part in enumerate(parts):
                                if i > 0 and part and part[0].isupper() and not part.isupper():
                                                    # First TitleCase (mixed case) part — sol ID is everything before
                                                    return '-'.join(parts[:i])
                                            # No TitleCase found — return the whole slug as-is (may be just the sol ID)
                                            if parts:
                                                            return parts[0]

                        # 3. version_number as last resort
                        ver = pursuit.get("version_number")
    if ver is not None:
                ver_str = str(ver).strip()
        if ver_str and ver_str not in ("0", "None", ""):
                        return ver_str

    return ""


def get_due_date(pursuit):
        """Return best available due date."""
    for field in ("proposal_due_date", "source_soughts_due_date", "solicitation_date"):
                val = pursuit.get(field)
        if val:
                        return str(val)[:10]
                return ""


def get_pursuits():
        """Fetch all pursuits from HigherGov, all pages."""
    items, page = [], 1
    while True:
                r = requests.get(
                    "https://www.highergov.com/api-external/pursuit/",
                    params={"api_key": HGOV_KEY, "page_size": 100, "page_number": page},
                    timeout=30,
    )
        data = r.json()
        batch = data.get("results", [])
        items += batch
        if not data.get("next") or not batch:
                        break
                    page += 1
    filtered = [p for p in items if p.get("pipeline_name") in PIPELINE_CONFIG]
    print(f"Fetched {len(items)} total pursuits, {len(filtered)} from tracked pipelines")
    return filtered


def get_all_opps(pipeline_id):
        """Fetch ALL existing opps in a GHL pipeline, paginating through all pages.

            Returns:
                    dict mapping opp_name -> opp dict
                            set of existing sol IDs
                                    set of existing opp IDs
                                        """
    opps_by_name = {}
    sol_ids = set()
    opp_ids = set()
    page = 1

    while True:
                r = requests.get(
                    "https://services.leadconnectorhq.com/opportunities/search",
                    headers=GHL_HEADERS,
                    params={
                                        "location_id": LOCATION_ID,
                                        "pipeline_id": pipeline_id,
                                        "limit": 100,
                                        "page": page,
                    },
                    timeout=30,
    )
        data = r.json()
        opps = data.get("opportunities", [])
        if not opps:
                        break

        for o in opps:
                        name = o.get("name", "")
                        opps_by_name[name] = o
                        opp_ids.add(o["id"])
                        for cf in (o.get("customFields") or []):
                                            if cf.get("id") == SOLICITATION_FIELD_ID and cf.get("fieldValue"):
                                                                    sol_ids.add(str(cf["fieldValue"]).strip())

                                    # Check if there are more pages
                                    meta = data.get("meta", {})
        total = meta.get("total", 0)
        fetched_so_far = page * 100
        if fetched_so_far >= total or len(opps) < 100:
                        break
        page += 1

    return opps_by_name, sol_ids, opp_ids


def get_or_create_contact(name):
        """Find or create a GHL contact with [HGov] tag."""
    first_name = name[:40]

    # Search for existing contact
    r = requests.get(
                "https://services.leadconnectorhq.com/contacts/",
                headers=GHL_HEADERS,
                params={"locationId": LOCATION_ID, "query": first_name, "limit": 5},
                timeout=30,
    )
    if r.status_code == 200:
                for c in r.json().get("contacts", []):
                                if c.get("firstName", "")[:40] == first_name and c.get("lastName") == "[HGov]":
                                                    return c.get("id")

                        # Create new contact
                        payload = {
                                    "firstName": first_name,
                                    "lastName": "[HGov]",
                                    "locationId": LOCATION_ID,
                                    "tags": ["highergov"],
                        }
    r = requests.post(
                "https://services.leadconnectorhq.com/contacts/",
                headers=GHL_HEADERS,
                json=payload,
                timeout=30,
    )
    if r.status_code in (200, 201):
                return r.json().get("contact", {}).get("id")

    print(f"  CONTACT ERR ({r.status_code}): {r.text[:200]}")
    return None


def update_opp_sol_id(opp_id, sol_id):
        """Write the Solicitation ID custom field to an existing GHL opp."""
    r = requests.put(
                f"https://services.leadconnectorhq.com/opportunities/{opp_id}",
                headers=GHL_HEADERS,
                json={"customFields": [{"id": SOLICITATION_FIELD_ID, "field_value": sol_id}]},
                timeout=30,
    )
    return r.status_code in (200, 201)


def backfill_sol_ids(pursuits):
        """Update existing GHL opps missing a Solicitation ID.

            Matches pursuits to opps by:
                1. opp name contains pursuit_name (exact or substring)
                    2. opp name stripped of [SOL-ID] prefix matches pursuit_name
                        """
    print("\nBackfilling Solicitation IDs on existing opps...")

    by_pipeline = defaultdict(list)
    for p in pursuits:
                by_pipeline[p.get("pipeline_name")].append(p)

    total_backfilled = 0

    for hgov_name, pipeline_pursuits in by_pipeline.items():
                config = PIPELINE_CONFIG[hgov_name]
        pipeline_id = config["pipeline_id"]

        opps_by_name, existing_sol_ids, _ = get_all_opps(pipeline_id)
        print(f"  Pipeline '{hgov_name}': {len(opps_by_name)} opps in GHL")

        # Build lookup: pursuit_name -> (sol_id, pursuit)
        pursuit_lookup = {}
        for p in pipeline_pursuits:
                        sol_id = get_sol_id(p)
                        pname = (p.get("pursuit_name") or "").strip()
                        if sol_id and pname:
                                            pursuit_lookup[pname] = sol_id

                    for opp_name, opp in opps_by_name.items():
                                    # Skip if already has a sol ID
                                    has_sol = any(
                                                        cf.get("id") == SOLICITATION_FIELD_ID and cf.get("fieldValue")
                                                        for cf in (opp.get("customFields") or [])
                                    )
                                    if has_sol:
                                                        continue

                                    # Try to match
                                    matched_sol = None

            # Strategy 1: strip [SOL-ID] prefix from opp name
            name_stripped = re.sub(r'^\[[^\]]+\]\s*', '', opp_name).strip()

            # Strategy 2: strip date suffix like " (05/20/26) RFQ [HGov]" from opp name
            name_clean = re.sub(r'\s*\(\d{2}/\d{2}/\d{2}\)\s*(RFQ|RFP|SOURCES SOUGHT|SS)?\s*(\[HGov\])?\s*$', '', opp_name, flags=re.IGNORECASE).strip()
            name_clean = re.sub(r'\s*\[HGov\]\s*$', '', name_clean).strip()
            name_stripped_clean = re.sub(r'^\[[^\]]+\]\s*', '', name_clean).strip()

            for candidate in [opp_name, name_stripped, name_clean, name_stripped_clean]:
                                if candidate in pursuit_lookup:
                                                        matched_sol = pursuit_lookup[candidate]
                                                        break

                            # Strategy 3: substring match (opp name contains pursuit name)
                            if not matched_sol:
                                                for pname, sol_id in pursuit_lookup.items():
                                                                        if pname and (pname in opp_name or pname in name_clean):
                                                                                                    matched_sol = sol_id
                                                                                                    break

                                                                if matched_sol:
                                                if update_opp_sol_id(opp["id"], matched_sol):
                                                                        total_backfilled += 1
                                                                        print(f"  BACKFILL: [{matched_sol}] {opp_name}")

    print(f"Backfill complete: {total_backfilled} opps updated")


def sync():
        pursuits = get_pursuits()
    created = skipped = errors = 0

    # Backfill sol IDs on existing opps missing them
    backfill_sol_ids(pursuits)

    by_pipeline = defaultdict(list)
    for p in pursuits:
                by_pipeline[p.get("pipeline_name")].append(p)

    for hgov_pipeline_name, pipeline_pursuits in by_pipeline.items():
                config = PIPELINE_CONFIG[hgov_pipeline_name]
        pipeline_id = config["pipeline_id"]
        entry_stage_id = config["entry_stage_id"]

        opps_by_name, existing_sol_ids, _ = get_all_opps(pipeline_id)
        existing_names = set(opps_by_name.keys())

        print(
                        f"\nPipeline '{hgov_pipeline_name}': {len(pipeline_pursuits)} in HGov, "
                        f"{len(existing_names)} already in GHL"
        )

        for p in pipeline_pursuits:
                        pursuit_name = (p.get("pursuit_name") or "Unnamed").strip()
            sol_id = get_sol_id(p)
            opp_name = f"[{sol_id}] {pursuit_name}" if sol_id else pursuit_name

            # Skip if already exists (check sol ID OR name)
            if sol_id and sol_id in existing_sol_ids:
                                skipped += 1
                continue
            if opp_name in existing_names:
                                skipped += 1
                continue
            # Also check if pursuit name (without sol prefix) is already in GHL
            if pursuit_name in existing_names:
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
                                headers=GHL_HEADERS,
                                json=payload,
                                timeout=30,
            )
            resp = r.json()
            if resp.get("id") or resp.get("opportunity", {}).get("id"):
                                created += 1
                due_str = f" (due {due_date})" if due_date else ""
                sol_str = f"[{sol_id}] " if sol_id else ""
                print(f"  CREATED: {sol_str}{pursuit_name}{due_str}")
else:
                errors += 1
                print(f"  CREATE ERR [{sol_id}] {pursuit_name}: {r.text[:120]}")

    print(f"\nSync complete: {created} created, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
        sync()
