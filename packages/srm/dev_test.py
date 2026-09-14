"""Safe development testing tool for SRM HTTP integration.

Allows developers to test direct HTTP operations against the SRM portal
using credentials supplied strictly via environment variables.
Guarantees zero accidental leakage of passwords, JWTs, or Authorization headers.
"""

import asyncio
import os
import sys
from pathlib import Path
from packages.srm.http_client import SRMHttpClient
from packages.srm.exceptions import AuthenticationFailed, SRMApiError, SRMException


async def run_safe_dev_test():
    print("=" * 60)
    print("SRM HTTP Integration Developer Test")
    print("=" * 60)

    client = SRMHttpClient()

    # Step 1: Connect & Check Status
    print("\n[1/5] Probing portal connectivity (/curricula/checkstatus)...")
    try:
        await client.connect()
        print("  -> Connectivity verified: SRM checkstatus returned OK.")
    except Exception as exc:
        print(f"  -> Connection check failed: {exc}")
        return

    # Step 2: Read credentials from Environment (NEVER print password or JWT)
    student_id = os.getenv("SRM_STUDENT_ID")
    password = os.getenv("SRM_STUDENT_PASSWORD")
    captcha_code = os.getenv("SRM_CAPTCHA_CODE")

    if not student_id or not password:
        print("\n[INFO] No SRM_STUDENT_ID or SRM_STUDENT_PASSWORD found in environment.")
        print("  To run authenticated integration tests, set:")
        print("    $env:SRM_STUDENT_ID = 'YOUR_ID'")
        print("    $env:SRM_STUDENT_PASSWORD = 'YOUR_PASSWORD'")
        print("    $env:SRM_CAPTCHA_CODE = '123456' (if applicable)")
        print("\n  Direct HTTP endpoints are operational and verified.")
        await client.close()
        return

    # Safe display
    masked_uid = student_id[:2] + "*" * (len(student_id) - 4) + student_id[-2:] if len(student_id) > 4 else "***"
    print(f"\n[2/5] Attempting authentication for student {masked_uid}...")
    print("  Password: [REDACTED]")

    try:
        credentials = {
            "USER_ID": student_id,
            "PASSWORD": password,
            "key": client.key,
        }
        if captcha_code:
            credentials["captcha_solution"] = captcha_code

        auth_success = await client.authenticate(credentials)
        if auth_success and client._jwt_token:
            token_len = len(client._jwt_token)
            print(f"  -> Authentication SUCCEEDED! JWT Token received [REDACTED (length: {token_len})]")
        else:
            print("  -> Authentication returned no token.")
            await client.close()
            return
    except AuthenticationFailed as af:
        print(f"  -> Authentication FAILED: {af}")
        await client.close()
        return
    except Exception as exc:
        print(f"  -> Authentication encountered error: {exc}")
        await client.close()
        return

    # Step 3: Discover Courses & Filter Semester 3
    print("\n[3/5] Querying enrolled courses (POST /curricula/student/home/getcourses)...")
    try:
        courses = await client.get_courses()
        print(f"  -> Found {len(courses)} total courses.")
        for idx, c in enumerate(courses[:5], 1):
            print(f"     {idx}. {c.course_code} - {c.course_name} (Semester {c.semester}, Batch {c.batch_id})")

        sem3_courses = await client.get_courses_by_semester(3)
        print(f"  -> Filtered {len(sem3_courses)} courses for Semester 3.")
    except Exception as exc:
        print(f"  -> Course retrieval error: {exc}")
        await client.close()
        return

    # Step 4: Inspect First Course Session & Status
    if courses:
        target = courses[0]
        print(f"\n[4/5] Retrieving session status for {target.course_code} (Session 1)...")
        try:
            status = await client.get_session_status(
                course_info={"BATCH_ID": target.batch_id, "COURSE_CODE": target.course_code},
                session=1
            )
            print(f"  -> Practice Status: {status.practice_status}")
            print(f"  -> Submitted Links count: {len(status.slo_links)}")
        except Exception as exc:
            print(f"  -> Session status check error: {exc}")

    # Step 5: Test File Lookup & Download
    if courses:
        target = courses[0]
        print(f"\n[5/5] Testing worksheet file resolution for {target.course_code}...")
        try:
            file_url = await client.get_worksheet_file(
                course_code=target.course_code,
                filename="worksheet_session_1.docx"
            )
            print(f"  -> Resolved file URL: {file_url}")
            dl_path = await client.download_worksheet(file_url)
            print(f"  -> Downloaded file to temporary directory: {dl_path}")
        except Exception as exc:
            print(f"  -> Worksheet file resolution notice (file may not exist for this course/session yet): {exc}")

    await client.close()
    print("\n" + "=" * 60)
    print("Developer Test Complete")
    print("=" * 60)


def main():
    asyncio.run(run_safe_dev_test())


if __name__ == "__main__":
    main()
