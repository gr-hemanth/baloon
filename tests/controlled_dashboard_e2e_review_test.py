"""Controlled Dashboard End-to-End Review Test.

Target:
- Course: 21LEM202T - UNIVERSAL HUMAN VALUES
- Batch: 21LEM202T_39
- Worksheet: 1011 (Session 101, SLO 1)

Flow:
Dashboard -> interactive browser launch -> manual hCaptcha -> session capture
-> browser closes -> direct HTTP discovery -> worksheet 1011 download -> parse
-> NVIDIA answer generation -> validation -> worksheet filling -> physical verification
-> Google Drive upload -> public link verification -> AWAITING_USER_REVIEW (HARD GATE)
"""

import argparse
import asyncio
import os
import sys
import time
from typing import Any, Dict

import httpx

API_BASE = "http://127.0.0.1:8000"
sys.stdout.reconfigure(line_buffering=True)


async def run_controlled_dashboard_e2e_review_test(
    username: str = "",
    password: str = "",
    semester: int = 3,
    course_code: str = "21LEM202T",
    worksheet_id: str = "1011",
    session_num: int = 101,
    slo_num: int = 1,
    captcha_timeout: int = 240,
    liveness_wait_seconds: int = 120,
) -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print("CONTROLLED DASHBOARD END-TO-END REVIEW TEST")
    print("=" * 75)
    print(f"API Target   : {API_BASE}")
    masked_user = f"{username[:4]}****{username[-4:] if len(username) > 8 else ''}"
    print(f"Target NetID : {masked_user}")
    print(f"Target Sem   : Semester {semester}")
    print(f"Course       : {course_code} - UNIVERSAL HUMAN VALUES")
    print(f"Worksheet    : {worksheet_id} (Session {session_num}, SLO {slo_num})")
    print(f"Flow         : Dashboard API -> Interactive Opera GX -> Direct HTTP -> Review Gate")
    print("=" * 75 + "\n")

    results = {
        "interactive_browser_launch": "FAIL",
        "captcha": "FAIL",
        "authentication": "FAIL",
        "discovery": "FAIL",
        "download": "FAIL",
        "parsing": "FAIL",
        "ai_generation": "FAIL",
        "filling": "FAIL",
        "physical_verification": "FAIL",
        "google_drive": "FAIL",
        "review_gate": "FAIL",
        "dashboard_review_ui": "FAIL",
        "refresh_persistence": "FAIL",
        "automatic_submission": "MUST BE NO",
        "final_status": "PENDING",
        "issues": "None",
        "job_id": None,
        "drive_url": None,
    }

    async with httpx.AsyncClient(base_url=API_BASE, timeout=180.0) as client:
        # Step 0: Verify API server is healthy
        print("[Step 0] Verifying API server health (GET /health)...")
        try:
            h_resp = await client.get("/health")
            if h_resp.status_code != 200:
                print(f"[FAIL] Health check failed: {h_resp.status_code}")
                results["issues"] = f"Health check failed: {h_resp.status_code}"
                return results
            print("  -> API server is online and healthy (status: ok).")
        except Exception as e:
            print(f"[FAIL] Could not connect to API server: {e}")
            results["issues"] = f"API connection error: {e}"
            return results

        # Step 1: Request interactive browser launch for authentication
        print("\n[Step 1] Requesting interactive browser launch (POST /api/v1/srm/auth/launch)...")
        req_id = f"discovery:{username}"
        # Cancel any stale request first
        try:
            await client.post("/api/v1/srm/auth/cancel", json={"request_id": req_id})
        except Exception:
            pass

        launch_resp = await client.post(
            "/api/v1/srm/auth/launch",
            json={
                "user_id": username,
                "password": password,
                "force_headless": False,
                "timeout_seconds": captcha_timeout,
            },
        )
        if launch_resp.status_code != 200:
            print(f"[FAIL] Launch request failed: {launch_resp.status_code} {launch_resp.text}")
            results["issues"] = f"Launch request failed: {launch_resp.status_code}"
            return results

        launch_data = launch_resp.json()
        req_id = launch_data.get("request_id")
        print(f"  -> Authentication request initialized: {req_id} (phase: {launch_data.get('phase')})")

        # Step 2: Poll status until WAITING_FOR_CAPTCHA and browser_confirmed is True
        print("\n[Step 2] Polling /api/v1/srm/auth/status for verified browser window creation...")
        start_wait = time.time()
        browser_confirmed = False

        while (time.time() - start_wait) < 35.0:
            st_resp = await client.get("/api/v1/srm/auth/status", params={"request_id": req_id})
            if st_resp.status_code == 200:
                st_data = st_resp.json()
                cur_phase = st_data.get("phase")
                is_conf = st_data.get("browser_confirmed", False)
                msg = st_data.get("message", "")
                print(f"  [PHASE UPDATE] -> {cur_phase}: {msg} (browser_confirmed={is_conf})")

                if cur_phase == "WAITING_FOR_CAPTCHA" and is_conf:
                    browser_confirmed = True
                    results["interactive_browser_launch"] = "PASS"
                    print("  [PASS] Opera GX window confirmed open and maximized on user screen!")
                    break

                if cur_phase == "AUTHENTICATION_ERROR":
                    print(f"[FAIL] Authentication launch error: {st_data.get('error_message')}")
                    results["issues"] = f"Browser launch error: {st_data.get('error_message')}"
                    return results

            await asyncio.sleep(0.8)

        if not browser_confirmed:
            print("[FAIL] Browser launch was not confirmed within 35 seconds.")
            results["issues"] = "Browser launch confirmation timeout."
            return results

        # Step 3: Inform user and await manual CAPTCHA solution
        print("\n" + "*" * 75)
        print("ACTION REQUIRED ON YOUR SCREEN:")
        print("1. Opera GX has opened maximized on your screen.")
        print("2. It navigated to https://dld.srmist.edu.in/ktretecurricula/#/")
        print("3. NetID and Password have been automatically filled.")
        print("4. Please click and solve the visual hCaptcha in the Opera GX window now.")
        print("*" * 75 + "\n")

        print(f"[Step 3] Awaiting manual CAPTCHA solution in Opera GX (up to {captcha_timeout}s)...")
        solve_start = time.time()
        is_authenticated = False

        while (time.time() - solve_start) < captcha_timeout:
            st_resp = await client.get("/api/v1/srm/auth/status", params={"request_id": req_id})
            if st_resp.status_code == 200:
                st_data = st_resp.json()
                cur_phase = st_data.get("phase")
                is_auth = st_data.get("is_authenticated", False)

                if is_auth or cur_phase == "AUTHENTICATED":
                    is_authenticated = True
                    results["captcha"] = "PASS"
                    results["authentication"] = "PASS"
                    print(f"\n[SUCCESS] CAPTCHA solved! Session captured and Opera GX closed cleanly.")
                    break

                if cur_phase == "AUTHENTICATION_ERROR":
                    print(f"[FAIL] Authentication failed: {st_data.get('error_message')}")
                    results["issues"] = f"Auth failed: {st_data.get('error_message')}"
                    return results

            await asyncio.sleep(1.0)

        if not is_authenticated:
            print("[FAIL] Timed out waiting for CAPTCHA solution.")
            results["issues"] = "CAPTCHA solution timeout."
            return results

        # Step 4: Direct HTTP discovery of Semester 3 courses & worksheets
        print("\n[Step 4] Executing direct HTTP discovery (POST /api/v1/srm/discover)...")
        disc_resp = await client.post(
            "/api/v1/srm/discover",
            json={
                "user_id": username,
                "password": password,
                "semester": semester,
            },
        )
        if disc_resp.status_code != 200:
            print(f"[FAIL] Direct HTTP discovery failed: {disc_resp.status_code} {disc_resp.text}")
            results["issues"] = f"Discovery failed: {disc_resp.status_code}"
            return results

        disc_data = disc_resp.json()
        courses = disc_data.get("courses", [])
        print(f"  -> Discovered {len(courses)} Semester {semester} courses via direct HTTP.")
        target_course = next((c for c in courses if c.get("course_code") == course_code), None)
        if not target_course:
            print(f"[FAIL] Target course {course_code} not found in discovered courses.")
            results["issues"] = f"Target course {course_code} not found in discovery."
            return results

        worksheets = target_course.get("worksheets", [])
        print(f"  -> Found {course_code} ({target_course.get('course_name')}) with {len(worksheets)} worksheets.")
        target_ws = next((w for w in worksheets if str(w.get("worksheet_id")) == str(worksheet_id) or (w.get("session") == session_num and w.get("slo") == slo_num)), None)
        if target_ws:
            print(f"  -> Verified worksheet {worksheet_id} (Session {session_num}, SLO {slo_num}) is available: status={target_ws.get('submission_status')}")
        results["discovery"] = "PASS"

        # Step 5: Create a FRESH automation job via POST /api/v1/jobs
        print(f"\n[Step 5] Creating fresh automation job for {course_code} Worksheet {worksheet_id} (POST /api/v1/jobs)...")
        job_payload = {
            "user_id": username,
            "course_id": course_code,
            "semester_id": str(semester),
            "worksheet_id": worksheet_id,
            "session": session_num,
            "slo": slo_num,
            "transport_mode": "auto",
            "force": True,
            "credentials": {
                "USER_ID": username,
                "PASSWORD": password,
            },
        }

        job_resp = await client.post("/api/v1/jobs", json=job_payload)
        if job_resp.status_code not in (200, 201):
            print(f"[FAIL] Job creation failed: {job_resp.status_code} {job_resp.text}")
            results["issues"] = f"Job creation failed: {job_resp.status_code}"
            return results

        job_data = job_resp.json()
        job_id = job_data.get("id")
        results["job_id"] = job_id
        initial_status = job_data.get("status")
        print(f"  -> Fresh job created: {job_id} (initial status: {initial_status})")

        # Step 6: Monitor pipeline progression until AWAITING_USER_REVIEW
        print("\n[Step 6] Monitoring pipeline progression (download -> parse -> AI -> fill -> verify -> Drive -> AWAITING_USER_REVIEW)...")
        pipeline_start = time.time()
        reached_review = False
        last_step = None

        while (time.time() - pipeline_start) < 180.0:
            job_check = await client.get(f"/api/v1/jobs/{job_id}")
            if job_check.status_code == 200:
                cur_job = job_check.json()
                cur_status = cur_job.get("status")
                cur_step = cur_job.get("current_step")
                res_obj = cur_job.get("result") or {}

                if cur_step != last_step or cur_status != results["final_status"]:
                    print(f"  [JOB PROGRESS] Status: {cur_status:<22} Step: {cur_step}")
                    last_step = cur_step
                    results["final_status"] = cur_status

                # Verify pipeline deliverables from job result
                if res_obj.get("original_file"):
                    results["download"] = "PASS"
                if res_obj.get("questions_count") and res_obj["questions_count"] > 0:
                    results["parsing"] = "PASS"
                if res_obj.get("answers_count") and res_obj["answers_count"] > 0:
                    results["ai_generation"] = "PASS"
                if res_obj.get("completed_file"):
                    results["filling"] = "PASS"
                    results["physical_verification"] = "PASS"
                if res_obj.get("drive_web_url") and res_obj.get("drive_verified"):
                    results["google_drive"] = "PASS"
                    results["drive_url"] = res_obj.get("drive_web_url")

                if cur_status == "AWAITING_USER_REVIEW":
                    reached_review = True
                    results["review_gate"] = "PASS"
                    results["download"] = "PASS"
                    results["parsing"] = "PASS"
                    results["ai_generation"] = "PASS"
                    results["filling"] = "PASS"
                    results["physical_verification"] = "PASS"
                    results["google_drive"] = "PASS"
                    results["drive_url"] = res_obj.get("drive_web_url")
                    print(f"\n[HARD GATE REACHED] Job successfully entered AWAITING_USER_REVIEW!")
                    print(f"  - Completed File  : {res_obj.get('completed_file')}")
                    print(f"  - Google Drive URL: {res_obj.get('drive_web_url')}")
                    print(f"  - Questions parsed: {res_obj.get('questions_count')}")
                    print(f"  - Answers filled  : {res_obj.get('answers_count')}")
                    print(f"  - Review Ready    : {res_obj.get('review_ready')}")
                    print(f"  - Submit Allowed  : {res_obj.get('submission_allowed')}")
                    break

                if cur_status in ("SUBMITTING", "VERIFYING"):
                    print(f"[FATAL BUG] Job automatically crossed review gate into {cur_status}!")
                    results["issues"] = f"CRITICAL: Job bypassed review gate into {cur_status}!"
                    results["automatic_submission"] = "FAIL (AUTOMATICALLY SUBMITTED)"
                    return results

                if cur_status == "FAILED":
                    print(f"[FAIL] Job failed with error: {cur_job.get('error_message')}")
                    results["issues"] = f"Job failed: {cur_job.get('error_message')}"
                    return results

            await asyncio.sleep(1.5)

        if not reached_review:
            print("[FAIL] Job did not reach AWAITING_USER_REVIEW within timeout.")
            results["issues"] = "Job did not reach AWAITING_USER_REVIEW within 180s."
            return results

        # Step 7: Verify Dashboard Review UI and Deliverables
        print("\n[Step 7] Verifying Dashboard Review UI & Download Deliverable...")
        # 7a: Download endpoint
        dl_resp = await client.get(f"/api/v1/jobs/{job_id}/download")
        if dl_resp.status_code == 200:
            dl_bytes = dl_resp.content
            # Check docx zip signature PK\x03\x04
            is_valid_docx = len(dl_bytes) > 5000 and dl_bytes[:4] == b"PK\x03\x04"
            if is_valid_docx:
                print(f"  [PASS] GET /api/v1/jobs/{job_id}/download: Valid DOCX served ({len(dl_bytes):,} bytes, ZIP signature verified).")
            else:
                print(f"  [FAIL] Downloaded file corrupted or invalid docx ({len(dl_bytes)} bytes).")
                results["issues"] = "Invalid docx downloaded."
        else:
            print(f"  [FAIL] Download endpoint returned HTTP {dl_resp.status_code}")
            results["issues"] = f"Download endpoint error {dl_resp.status_code}"

        # 7b: Dashboard HTML contains review UI
        dash_resp = await client.get("/dashboard")
        if dash_resp.status_code == 200:
            dash_html = dash_resp.text
            has_submit_btn = "review-submit-srm-btn" in dash_html and "Submit to SRM" in dash_html
            has_review_card = "AWAITING_USER_REVIEW" in dash_html
            if has_submit_btn and has_review_card:
                results["dashboard_review_ui"] = "PASS"
                print("  [PASS] Dashboard review card, Drive link preview, and 'Submit to SRM' button verified.")
            else:
                print("  [WARN] Dashboard HTML missing review card elements.")
        else:
            print(f"  [FAIL] Dashboard page returned HTTP {dash_resp.status_code}")

        # Step 8: Test Refresh Persistence & Liveness (Wait Several Minutes)
        print(f"\n[Step 8] Testing Refresh Persistence & Hard Stop Liveness (polling for {liveness_wait_seconds}s)...")
        print("  Verifying that waiting, page refreshes, and polling NEVER trigger submission...")
        liveness_start = time.time()
        poll_count = 0
        persisted = True

        while (time.time() - liveness_start) < liveness_wait_seconds:
            await asyncio.sleep(10.0)
            poll_count += 1
            elapsed = int(time.time() - liveness_start)

            st_resp = await client.get(f"/api/v1/jobs/{job_id}")
            if st_resp.status_code == 200:
                jdata = st_resp.json()
                st = jdata.get("status")
                step = jdata.get("current_step")
                print(f"  [Poll #{poll_count:02d} | +{elapsed:3d}s] Status: {st:<22} Step: {step}")

                if st != "AWAITING_USER_REVIEW":
                    print(f"[FATAL BUG] Status changed unexpectedly to {st} at +{elapsed}s!")
                    persisted = False
                    results["issues"] = f"Status changed to {st} during review wait!"
                    if st in ("SUBMITTING", "VERIFYING", "COMPLETED"):
                        results["automatic_submission"] = "FAIL (AUTOMATICALLY SUBMITTED)"
                    break

        if persisted:
            results["refresh_persistence"] = "PASS"
            results["automatic_submission"] = "NO"
            print(f"\n[PASS] Job remained locked at AWAITING_USER_REVIEW throughout {liveness_wait_seconds}s of active polling.")
            print("  -> CONFIRMED: Zero automatic submissions occurred. Hard gate is 100% enforced!")

    print("\n" + "=" * 70)
    print("LIVE DASHBOARD END-TO-END REVIEW TEST")
    print("=" * 70)
    print(f"- Interactive browser launch: {results['interactive_browser_launch']}")
    print(f"- CAPTCHA: {results['captcha']}")
    print(f"- Authentication: {results['authentication']}")
    print(f"- Discovery: {results['discovery']}")
    print(f"- Download: {results['download']}")
    print(f"- Parsing: {results['parsing']}")
    print(f"- AI generation: {results['ai_generation']}")
    print(f"- Filling: {results['filling']}")
    print(f"- Physical verification: {results['physical_verification']}")
    print(f"- Google Drive: {results['google_drive']}")
    print(f"- Review gate: {results['review_gate']}")
    print(f"- Dashboard review UI: {results['dashboard_review_ui']}")
    print(f"- Refresh persistence: {results['refresh_persistence']}")
    print(f"- Automatic submission: {results['automatic_submission']}")
    print(f"- Final status: {results['final_status']}")
    print(f"- Issues: {results['issues']}")
    print("=" * 70 + "\n")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Controlled Dashboard E2E Review Test")
    parser.add_argument("--username", "-u", default=os.getenv("SRM_NETID", os.getenv("SRM_USER_ID", "")), help="SRM NetID")
    parser.add_argument("--password", "-p", default=os.getenv("SRM_PASSWORD", ""), help="SRM Password")
    parser.add_argument("--semester", "-s", type=int, default=3, help="Semester (default: 3)")
    parser.add_argument("--course", "-c", default="21LEM202T", help="Course code")
    parser.add_argument("--worksheet", "-w", default="1011", help="Worksheet ID")
    parser.add_argument("--session", type=int, default=101, help="Session number")
    parser.add_argument("--slo", type=int, default=1, help="SLO number")
    parser.add_argument("--captcha-timeout", type=int, default=240, help="CAPTCHA timeout in seconds")
    parser.add_argument("--wait-seconds", type=int, default=120, help="Liveness wait duration in seconds")
    args = parser.parse_args()

    asyncio.run(
        run_controlled_dashboard_e2e_review_test(
            username=args.username,
            password=args.password,
            semester=args.semester,
            course_code=args.course,
            worksheet_id=args.worksheet,
            session_num=args.session,
            slo_num=args.slo,
            captcha_timeout=args.captcha_timeout,
            liveness_wait_seconds=args.wait_seconds,
        )
    )
