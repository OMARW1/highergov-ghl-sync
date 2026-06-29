import os
import requests
from collections import defaultdict

HGOV_KEY = os.environ["HIGHERGOV_API_KEY"]
GHL_TOKEN = os.environ["GHL_API_KEY"]
LOCATION_ID = "nN5rGX4FAVMJFzv4Qvdy"

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
    r = requests.get("https://services.leadconnectorhq.com/opportunities/search",
        headers={"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28"},
        params={"location_id": LOCATION_ID, "pipeline_id": pipeline_id, "limit": 100})
    return {o["name"]: o for o in r.json().get("opportunities", [])}

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
        existing = get_existing_opps(pipeline_id)
        print(f"\nSyncing '{hgov_name}' -> GHL pipeline {pipeline_id}")
        print(f"  {len(pipeline_pursuits)} pursuits from HGov, {len(existing)} already in GHL")

        for p in pipeline_pursuits:
            name = p.get("pursuit_name") or "Unnamed"
            stage = map_stage(p.get("stage_name", ""))
            stage_id = stage_ids[stage]
            status = "won" if stage == "Closed" else "open"
            value = float(p.get("est_value") or p.get("weighted_value") or 0)
            payload = {"name": name, "pipelineId": pipeline_id, "locationId": LOCATION_ID,
                       "pipelineStageId": stage_id, "status": status, "monetaryValue": value}

            if name in existing:
                r = requests.put(f"https://services.leadconnectorhq.com/opportunities/{existing[name]['id']}",
                    headers=headers, json=payload)
                if r.json().get("opportunity"): updated += 1
                else: errors += 1; print(f"  UPDATE ERR {name}: {r.text[:100]}")
            else:
                cid = create_contact(name)
                if not cid: errors += 1; continue
                payload["contactId"] = cid
                r = requests.post("https://services.leadconnectorhq.com/opportunities/",
                    headers=headers, json=payload)
                if r.json().get("id"): created += 1
                else: errors += 1; print(f"  CREATE ERR {name}: {r.text[:100]}")

    print(f"\nSync complete: {created} created, {updated} updated, {errors} errors (of {len(pursuits)} total)")

if __name__ == "__main__":
    sync()
