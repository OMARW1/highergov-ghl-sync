import os
import requests

HGOV_KEY    = os.environ["HIGHERGOV_API_KEY"]
GHL_TOKEN   = os.environ["GHL_API_KEY"]
LOCATION_ID = "nN5rGX4FAVMJFzv4Qvdy"
PIPELINE_ID = "0GHLxoIY3MkwhWg4p10Y"

STAGE_IDS = {
    "New Lead":      "f111064d-0b33-4b7a-a8a2-4b43adcc9374",
    "Contacted":     "dadbc559-24e4-4e82-ab1b-c6896c097b0c",
    "Qualified":     "64b2fd2c-8d82-4ef6-9fe3-8905f96c5e9d",
    "Proposal Sent": "54ef88d1-97ff-4d6c-b468-4d6a5ddd6e6a",
    "Negotiation":   "1d7538c7-dffe-4f7c-b522-766980f65759",
    "Closed":        "b6722b0e-b8b9-47f8-8267-2c6933ee619c",
}

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
    return items

def get_existing_opps():
    r = requests.get("https://services.leadconnectorhq.com/opportunities/search",
        headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28"},
        params={"location_id": LOCATION_ID, "pipeline_id": PIPELINE_ID, "limit": 100})
    return {o["name"]: o for o in r.json().get("opportunities", [])}

def create_contact(name):
    r = requests.post("https://services.leadconnectorhq.com/contacts/",
        headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28", "Content-Type": "application/json"},
        json={"firstName": name[:40], "lastName": "[HGov]", "locationId": LOCATION_ID, "tags": ["highergov"]})
    return r.json().get("contact", {}).get("id")

def sync():
    pursuits = get_pursuits()
    existing = get_existing_opps()
    headers  = {"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28", "Content-Type": "application/json"}
    created = updated = errors = 0

    for p in pursuits:
        name     = p.get("pursuit_name") or "Unnamed"
        stage    = map_stage(p.get("stage_name", ""))
        stage_id = STAGE_IDS[stage]
        status   = "won" if stage == "Closed" else "open"
        value    = float(p.get("est_value") or p.get("weighted_value") or 0)
        payload  = {"name": name, "pipelineId": PIPELINE_ID, "locationId": LOCATION_ID,
                    "pipelineStageId": stage_id, "status": status, "monetaryValue": value}

        if name in existing:
            r = requests.put(f"https://services.leadconnectorhq.com/opportunities/{existing[name]['id']}",
                headers=headers, json=payload)
            if r.json().get("opportunity"): updated += 1
            else: errors += 1; print(f"UPDATE ERR {name}: {r.text[:100]}")
        else:
            cid = create_contact(name)
            if not cid: errors += 1; continue
            payload["contactId"] = cid
            r = requests.post("https://services.leadconnectorhq.com/opportunities/",
                headers=headers, json=payload)
            if r.json().get("id"): created += 1
            else: errors += 1; print(f"CREATE ERR {name}: {r.text[:100]}")

    print(f"Sync complete: {created} created, {updated} updated, {errors} errors (of {len(pursuits)} pursuits)")

if __name__ == "__main__":
    sync()
