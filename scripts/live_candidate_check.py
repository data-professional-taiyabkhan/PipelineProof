"""Smoke test for a deployed site: the candidate journey inside the assessor demo (synthetic data only).

    python scripts/live_candidate_check.py https://<your-app>.modal.run
"""
import io
import re
import sys
import time
import zipfile

import httpx

BASE = sys.argv[1].rstrip("/")
emp = httpx.Client(base_url=BASE, follow_redirects=True, timeout=60)
r = emp.post("/demo/enter")
assert r.status_code == 200 and "Northwind Broadband" in r.text, r.status_code
csrf = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
dash = emp.get("/app").text
aid = re.search(r'href="/app/assessments/([0-9a-f-]{36})"><strong>Machine learning engineer take-home \(sample\)', dash).group(1)
r = emp.post(f"/app/assessments/{aid}/invites", data={"candidate_name": "Live check (synthetic)", "csrf_token": csrf})
assert r.status_code == 200, r.status_code
token = re.findall(r'data-copy-text="[^"]*/c/([A-Za-z0-9_-]{20,})"', r.text)[-1]
print("invite created")

cand = httpx.Client(base_url=BASE, follow_redirects=True, timeout=60)
assert "Hello Live check" in cand.get(f"/c/{token}").text
assert cand.post(f"/c/{token}/consent", data={"agree": "1"}).status_code == 200
csv = cand.get(f"/c/{token}/train.csv")
assert csv.status_code == 200 and csv.text.startswith("row_id,customer_id")
tz = cand.get(f"/c/{token}/starter-template.zip")
names = zipfile.ZipFile(io.BytesIO(tz.content)).namelist()
print("consented; train.csv", len(csv.content) // 1024, "KB; template files:", len(names))

zip_bytes = emp.get("/app/demo-runs/p01_clean.zip").content
t0 = time.time()
r = cand.post(f"/c/{token}/upload", files={"file": ("my_submission.zip", zip_bytes, "application/zip")})
assert r.status_code == 200, r.text[:300]
while True:
    page = cand.get(f"/c/{token}").text
    if "passed the check" in page:
        print(f"pre-flight passed in {time.time() - t0:.0f}s")
        break
    if "found problems" in page:
        print("pre-flight FAILED:", re.findall(r"<li>(.*?)</li>", page)[:5])
        sys.exit(1)
    if time.time() - t0 > 600:
        print("pre-flight timed out")
        sys.exit(1)
    time.sleep(5)
sid = re.search(rf'action="/c/{token}/submit/([0-9a-f-]{{36}})"', page).group(1)
r = cand.post(f"/c/{token}/submit/{sid}", data={"confirm": "1"})
assert r.status_code == 200 and "being evaluated" in r.text
t1 = time.time()
while True:
    page = cand.get(f"/c/{token}").text
    if "Evaluation complete" in page:
        print(f"evaluated in {time.time() - t1:.0f}s")
        break
    if time.time() - t1 > 900:
        print("evaluation timed out")
        sys.exit(1)
    time.sleep(6)
assert "Unseen customers" not in page, "candidate must not see results before feedback is released"
rep = emp.get(f"/app/submissions/{sid}").text
rec = re.search(r"<h2>(No blocking findings|Review findings before advancing|Could not be scored)</h2>", rep)
print("employer report:", rec.group(1) if rec else "not found", "| overall:", re.search(r'aria-label="Overall score (\d+)', rep).group(1))
print("LIVE CANDIDATE CHECK OK")
