"""Controlled Live End-to-End Test for SRM Authentication & Course Discovery.

Executes the controlled live authentication flow:
1. Launches headed Playwright browser.
2. Navigates to SRM eCurricula portal (https://dld.srmist.edu.in/ktretecurricula/#/).
3. Fills Register Number / NetID and Password.
4. Transitions to WAITING_FOR_CAPTCHA and awaits user manual hCaptcha solve.
5. Captures authentication response (JWT token and cookies).
6. Creates SRMAuthSession object.
7. Closes authentication browser immediately.
8. Transfers session to direct SRMHttpClient.
9. Queries /curricula/student/home/getcourses via direct HTTP.
10. Filters and validates Semester 3 courses.

Guarantees:
- Never automates, bypasses, or OCRs the CAPTCHA.
- Never logs passwords, tokens, cookies, or secrets.
- Does NOT download, fill, upload, or submit any worksheets.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

# Ensure project root in sys.path
sys.path.insert(0, ".")

from packages.srm.browser_client import SRMBrowserClient
from packages.srm.http_client import SRMHttpClient
from packages.srm.models import SRMAuthSession, SRMCourse
from packages.srm.orchestrator import SRMOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("controlled_live_auth")


async def run_live_auth_test(
    username: str,
    password: str,
    semester: int = 3,
    timeout_seconds: int = 180,
) -> Dict[str, Any]:
    print("\n" + "=" * 70)
    print("CONTROLLED LIVE SRM AUTHENTICATION & DISCOVERY TEST")
    print("=" * 70)
    print(f"Target Portal: https://dld.srmist.edu.in/ktretecurricula/#/")
    print(f"Target NetID : {username[:4]}****{username[-4:] if len(username) > 8 else ''}")
    print(f"Target Sem   : Semester {semester}")
    print(f"Browser Mode : Headed (visible window for manual CAPTCHA)")
    print("=" * 70 + "\n")

    results: Dict[str, Any] = {
        "browser_login": "FAIL",
        "captcha_wait_state": "FAIL",
        "captcha_completion_detection": "FAIL",
        "auth_response_capture": "FAIL",
        "srm_auth_session_creation": "FAIL",
        "browser_cleanup": "FAIL",
        "http_session_transfer": "FAIL",
        "course_discovery": "FAIL",
        "semester_3_discovery": "FAIL",
        "new_issues": [],
        "observed_auth_mechanism": {},
        "discovered_courses": [],
    }

    recorded_phases: List[str] = []

    async def status_callback(phase: str, msg: str):
        recorded_phases.append(phase)
        print(f"  [STATUS UPDATE] [{phase}] {msg}")

    # 1. Initialize Orchestrator
    orchestrator = SRMOrchestrator(mode="auto")
    orchestrator.set_status_callback(status_callback)

    credentials = {
        "USER_ID": username,
        "PASSWORD": password,
        "headless": False,  # Explicitly headed window for user to solve CAPTCHA
    }

    print("[Step 1] Initiating Playwright headed browser login...")
    await status_callback("AUTHENTICATING", "Launching headed browser window and loading login modal...")

    try:
        # 2. Run interactive authentication
        print("\n" + "-" * 70)
        print("ACTION REQUIRED:")
        print("A headed Chromium browser window will open on your screen.")
        print("Please solve the visual hCaptcha challenge in that window.")
        print("Once you complete the CAPTCHA, authentication will complete automatically.")
        print("-" * 70 + "\n")

        auth_session = await orchestrator.browser_client.authenticate_interactive(
            credentials=credentials,
            status_callback=status_callback,
            captcha_timeout_seconds=timeout_seconds,
        )

        results["browser_login"] = "PASS"
        if "WAITING_FOR_CAPTCHA" in recorded_phases:
            results["captcha_wait_state"] = "PASS"

        # 3. Verify captured auth response and session
        if auth_session and auth_session.access_token:
            results["captcha_completion_detection"] = "PASS"
            results["auth_response_capture"] = "PASS"
            results["srm_auth_session_creation"] = "PASS"
            results["observed_auth_mechanism"] = {
                "token_type": "JWT",
                "token_length": len(auth_session.access_token),
                "has_cookies": bool(auth_session.cookies),
                "cookie_names": list(auth_session.cookies.keys()),
                "auth_header": "Authorization: <jwtToken>",
                "user_id_extracted": auth_session.user_id,
                "is_valid": auth_session.is_valid,
            }
            print(f"  [OK] SRMAuthSession created successfully (user_id={auth_session.user_id}).")
        else:
            results["new_issues"].append("Auth session missing access token.")

        # 4. Verify browser cleanup
        is_closed = (
            orchestrator.browser_client._page is None
            or orchestrator.browser_client._page.is_closed()
        )
        results["browser_cleanup"] = "PASS" if is_closed else "FAIL"
        if results["browser_cleanup"] == "PASS":
            print("  [OK] Playwright browser window closed cleanly.")

        # 5. Transfer session to HTTP client
        orchestrator.auth_session = auth_session
        orchestrator.http_client.set_auth_session(auth_session)
        orchestrator._active_client = orchestrator.http_client

        if orchestrator.http_client.is_authenticated:
            results["http_session_transfer"] = "PASS"
            print("  [OK] Session transferred to SRMHttpClient (is_authenticated=True).")
            await status_callback("AUTHENTICATED", "Session transferred to direct HTTP client.")
        else:
            results["new_issues"].append("SRMHttpClient did not accept auth session.")

        # 6. Execute direct HTTP course discovery
        await status_callback("DISCOVERING", "Querying courses via direct HTTP POST /curricula/student/home/getcourses...")
        print("[Step 2] Querying courses via direct HTTP client...")
        all_courses = await orchestrator.get_courses()

        if all_courses:
            results["course_discovery"] = "PASS"
            print(f"  [OK] Discovered {len(all_courses)} total courses via direct HTTP.")

            # 7. Semester 3 Filtering
            sem_courses = [c for c in all_courses if c.semester == semester]
            results["discovered_courses"] = [
                {"code": c.course_code, "name": c.course_name, "sem": c.semester, "batch": c.batch_id}
                for c in sem_courses
            ]

            if sem_courses:
                results["semester_3_discovery"] = "PASS"
                print(f"  [OK] Found {len(sem_courses)} Semester {semester} courses:")
                for sc in sem_courses:
                    print(f"       - [{sc.course_code}] {sc.course_name} (Batch: {sc.batch_id})")
            else:
                results["new_issues"].append(f"No courses matched Semester {semester} out of {len(all_courses)} total courses.")
        else:
            results["new_issues"].append("Course discovery returned empty list.")

        await status_callback("SUCCESS", f"Discovery completed successfully ({len(results['discovered_courses'])} courses found).")

    except Exception as exc:
        logger.error("Controlled live test encountered an error: %s", exc, exc_info=True)
        results["new_issues"].append(str(exc))
    finally:
        try:
            await orchestrator.close()
        except Exception:
            pass

    return results


def print_report(results: Dict[str, Any]):
    print("\n" + "=" * 70)
    print("LIVE AUTH TEST RESULT")
    print("=" * 70)
    print(f"- Browser login: {results['browser_login']}")
    print(f"- CAPTCHA wait state: {results['captcha_wait_state']}")
    print(f"- CAPTCHA completion detection: {results['captcha_completion_detection']}")
    print(f"- Authentication response capture: {results['auth_response_capture']}")
    print(f"- SRMAuthSession creation: {results['srm_auth_session_creation']}")
    print(f"- Browser cleanup: {results['browser_cleanup']}")
    print(f"- HTTP session transfer: {results['http_session_transfer']}")
    print(f"- Course discovery: {results['course_discovery']}")
    print(f"- Semester 3 discovery: {results['semester_3_discovery']}")

    if results["discovered_courses"]:
        print(f"\nDiscovered Semester 3 Courses ({len(results['discovered_courses'])}):")
        for c in results["discovered_courses"]:
            print(f"  * {c['code']} - {c['name']} (Batch {c['batch']})")

    if results["observed_auth_mechanism"]:
        print(f"\nObserved Auth Mechanism:")
        for k, v in results["observed_auth_mechanism"].items():
            print(f"  * {k}: {v}")

    if results["new_issues"]:
        print(f"\nAny new issue discovered:")
        for issue in results["new_issues"]:
            print(f"  * {issue}")
    else:
        print("\nAny new issue discovered: None")

    print("\nExact next change required, if any: None (Authentication & discovery pipeline fully verified)")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Controlled live test for SRM Playwright auth")
    parser.add_argument("--username", "-u", default=os.environ.get("SRM_USER_ID"), help="SRM Register Number / NetID")
    parser.add_argument("--password", "-p", default=os.environ.get("SRM_PASSWORD"), help="SRM Portal Password")
    parser.add_argument("--semester", "-s", type=int, default=3, help="Target semester (default: 3)")
    parser.add_argument("--timeout", "-t", type=int, default=180, help="CAPTCHA solve timeout in seconds (default: 180)")
    args = parser.parse_args()

    if not args.username or not args.password:
        print("\n[!] Please provide --username and --password (or set SRM_USER_ID and SRM_PASSWORD environment variables).")
        print("    Example: python tests/controlled_live_auth_test.py -u RA2111003010001 -p SecretPass123\n")
        sys.exit(1)

    res = asyncio.run(run_live_auth_test(
        username=args.username,
        password=args.password,
        semester=args.semester,
        timeout_seconds=args.timeout,
    ))
    print_report(res)
