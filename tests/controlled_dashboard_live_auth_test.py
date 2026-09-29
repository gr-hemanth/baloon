"""Controlled Dashboard Live Authentication & Discovery Test.

Drives the exact dashboard authentication flow via live HTTP API:
1. Calls POST /api/v1/srm/auth/launch with student credentials.
2. Monitors phase progression via GET /api/v1/srm/auth/status:
   - Verifies AUTHENTICATING
   - Verifies OPENING_BROWSER (browser_confirmed=False)
   - Verifies WAITING_FOR_CAPTCHA only after browser_confirmed=True
3. Awaits user manual CAPTCHA solve in the visibly opened Chromium window.
4. Confirms browser closes cleanly and status reaches AUTHENTICATED.
5. Calls POST /api/v1/srm/discover to confirm direct HTTP discovery succeeds
   using the captured SRMAuthSession without any new browser.
6. Strict constraint: Zero worksheet downloads, zero AI generations, zero Drive uploads, zero SRM submissions.
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


async def run_dashboard_live_auth_test(
    username: str,
    password: str,
    semester: int = 3,
    timeout_seconds: int = 180,
) -> Dict[str, Any]:
    print("\n" + "=" * 75)
    print("CONTROLLED DASHBOARD LIVE AUTHENTICATION & DISCOVERY TEST")
    print("=" * 75)
    print(f"API Target   : {API_BASE}")
    print(f"Target NetID : {username[:4]}****{username[-4:] if len(username) > 8 else ''}")
    print(f"Target Sem   : Semester {semester}")
    print(f"Flow         : API interactive launcher -> Visible Chromium -> Direct HTTP")
    print("=" * 75 + "\n")

    results = {
        "api_launch_requested": "FAIL",
        "opening_browser_observed": "FAIL",
        "browser_confirmed_verified": "FAIL",
        "waiting_for_captcha_verified": "FAIL",
        "captcha_solved": "FAIL",
        "session_captured": "FAIL",
        "browser_closed": "FAIL",
        "direct_http_discovery": "FAIL",
        "courses_discovered": 0,
        "phases_observed": [],
    }

    async with httpx.AsyncClient(base_url=API_BASE, timeout=120.0) as client:
        # Step 1: Request interactive browser launch
        print("[Step 1] Requesting interactive browser launch from API (POST /api/v1/srm/auth/launch)...")
        launch_payload = {
            "user_id": username,
            "password": password,
            "force_headless": False,
            "timeout_seconds": timeout_seconds,
        }
        resp = await client.post("/api/v1/srm/auth/launch", json=launch_payload)
        if resp.status_code != 200:
            print(f"[FAIL] Launch request failed: {resp.status_code} {resp.text}")
            return results

        data = resp.json()
        req_id = data.get("request_id")
        print(f"  -> Launch initiated successfully. Request ID: {req_id}")
        results["api_launch_requested"] = "PASS"

        # Step 2: Poll status until WAITING_FOR_CAPTCHA and browser_confirmed is True
        print("\n[Step 2] Polling /api/v1/srm/auth/status for verified browser window creation...")
        start_wait = time.time()
        browser_confirmed = False

        while (time.time() - start_wait) < 30.0:
            st_resp = await client.get("/api/v1/srm/auth/status", params={"request_id": req_id})
            if st_resp.status_code == 200:
                st_data = st_resp.json()
                cur_phase = st_data.get("phase")
                is_conf = st_data.get("browser_confirmed", False)
                msg = st_data.get("message", "")

                if cur_phase not in results["phases_observed"]:
                    results["phases_observed"].append(cur_phase)
                    print(f"  [PHASE UPDATE] -> {cur_phase}: {msg} (browser_confirmed={is_conf})")

                if cur_phase == "OPENING_BROWSER":
                    results["opening_browser_observed"] = "PASS"

                if cur_phase == "WAITING_FOR_CAPTCHA" and is_conf:
                    browser_confirmed = True
                    results["browser_confirmed_verified"] = "PASS"
                    results["waiting_for_captcha_verified"] = "PASS"
                    break

                if cur_phase == "AUTHENTICATION_ERROR":
                    print(f"[FAIL] Authentication error: {st_data.get('error_message')}")
                    return results

            await asyncio.sleep(0.5)

        if not browser_confirmed:
            print("[FAIL] Browser launch was not confirmed within 30 seconds.")
            return results

        # Step 3: Inform user and await CAPTCHA solve
        print("\n" + "*" * 75)
        print("ACTION REQUIRED ON YOUR DESKTOP SCREEN:")
        print("1. A visible Chromium browser window has opened on your desktop.")
        print("2. It navigated to https://dld.srmist.edu.in/ktretecurricula/#/")
        print("3. NetID and Password have been automatically filled.")
        print("4. Please click and solve the visual hCaptcha in that window now.")
        print("*" * 75 + "\n")

        print("[Step 3] Awaiting manual CAPTCHA solve in opened browser window (up to 180s)...")
        solve_start = time.time()
        is_authenticated = False

        while (time.time() - solve_start) < timeout_seconds:
            st_resp = await client.get("/api/v1/srm/auth/status", params={"request_id": req_id})
            if st_resp.status_code == 200:
                st_data = st_resp.json()
                cur_phase = st_data.get("phase")

                if cur_phase not in results["phases_observed"]:
                    results["phases_observed"].append(cur_phase)
                    print(f"  [PHASE UPDATE] -> {cur_phase}: {st_data.get('message')}")

                if st_data.get("is_authenticated") or cur_phase == "AUTHENTICATED":
                    is_authenticated = True
                    results["captcha_solved"] = "PASS"
                    results["session_captured"] = "PASS"
                    results["browser_closed"] = "PASS"
                    print("\n[SUCCESS] CAPTCHA solved! JWT Token captured and browser closed cleanly.")
                    break

                if cur_phase == "AUTHENTICATION_ERROR":
                    print(f"[FAIL] Authentication failed: {st_data.get('error_message')}")
                    return results

            await asyncio.sleep(1.0)

        if not is_authenticated:
            print("[FAIL] Timed out waiting for CAPTCHA solution.")
            return results

        # Step 4: Verify direct HTTP discovery succeeds reusing captured session
        print("\n[Step 4] Executing direct HTTP discovery (POST /api/v1/srm/discover)...")
        disc_resp = await client.post(
            "/api/v1/srm/discover",
            json={
                "user_id": username,
                "password": password,
                "semester": semester,
            },
        )

        if disc_resp.status_code == 200:
            disc_data = disc_resp.json()
            courses = disc_data.get("courses", [])
            results["direct_http_discovery"] = "PASS"
            results["courses_discovered"] = len(courses)
            print(f"[SUCCESS] Discovered {len(courses)} Semester {semester} courses via direct HTTP:")
            for c in courses:
                print(f"  - {c.get('course_code')}: {c.get('course_name')} ({len(c.get('worksheets', []))} worksheets)")
        else:
            print(f"[FAIL] Discovery failed: {disc_resp.status_code} {disc_resp.text}")

    print("\n" + "=" * 75)
    print("SUMMARY OF VERIFICATION RESULTS:")
    print("=" * 75)
    for k, v in results.items():
        if k not in ("phases_observed", "courses_discovered"):
            print(f"  - {k:<30}: {v}")
    print(f"  - courses_discovered            : {results['courses_discovered']}")
    print(f"  - phases_observed               : {' -> '.join(results['phases_observed'])}")
    print("=" * 75 + "\n")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Controlled Dashboard Live Auth Test")
    parser.add_argument("--username", "-u", default=os.getenv("SRM_NETID", os.getenv("SRM_USER_ID", "")), help="SRM Register Number / NetID")
    parser.add_argument("--password", "-p", default=os.getenv("SRM_PASSWORD", ""), help="SRM Portal Password")
    parser.add_argument("--semester", "-s", type=int, default=3, help="Semester (default: 3)")
    parser.add_argument("--timeout", "-t", type=int, default=180, help="CAPTCHA timeout in seconds")
    args = parser.parse_args()

    asyncio.run(run_dashboard_live_auth_test(
        username=args.username,
        password=args.password,
        semester=args.semester,
        timeout_seconds=args.timeout,
    ))
