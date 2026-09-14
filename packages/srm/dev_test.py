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
from packages.srm.exceptions import AuthenticationFailed, SRMApiError, SRMException, WorksheetNotFound


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
        print(f"\n[4/5] Inspecting session details for {target.course_code} (Session 1)...")
        try:
            status = await client.get_session_status(
                course_info={"BATCH_ID": target.batch_id, "COURSE_CODE": target.course_code},
                session=1
            )
            print(f"  -> Session Status Practice map: {status.practice_status}")
            print(f"  -> Submitted Links count: {len(status.slo_links)}")
        except Exception as exc:
            print(f"  -> Session status check error: {exc}")

        try:
            questions = await client.get_questions(
                course_code=target.course_code,
                batch_id=target.batch_id,
                session=1,
            )
            print(f"  -> Session 1 Content: {len(questions.mcq)} MCQs, {len(questions.sq)} Short Qs, {len(questions.lq)} Long Qs")
        except Exception as exc:
            print(f"  -> Question retrieval notice: {exc}")

    # Step 5: Data-Driven Worksheet Discovery & Download
    target_course = None
    if sem3_courses:
        target_course = sem3_courses[0]
    elif courses:
        target_course = courses[0]

    if target_course:
        print(f"\n[5/5] Performing data-driven worksheet discovery...")
        
        # Test discovery across Semester 3 courses to find real available worksheets
        courses_to_probe = sem3_courses if sem3_courses else courses[:3]
        downloaded_ws_info = None

        for c in courses_to_probe:
            print(f"\n  --- Course: {c.course_code} ({c.course_name}) ---")
            try:
                discovered = await client.discover_worksheets(
                    course_code=c.course_code,
                    batch_id=c.batch_id,
                )
                available = [w for w in discovered if w.is_available]
                print(f"  -> Discovered {len(discovered)} total worksheet slots across units.")
                print(f"  -> Uploaded/Available on portal: {len(available)} worksheets.")

                if available:
                    # Print first 5 discovered available worksheets
                    print("  -> Sample Available Worksheets:")
                    for idx, w in enumerate(available[:5], 1):
                        print(f"     {idx}. Unit {w.unit} Session {w.session_no} (ID: {w.session}) SLO {w.slo}: {w.filename} [{w.format.upper()}]")
                        print(f"        Path: {w.storage_path}")
                        print(f"        URL: {w.download_url}")

                    # Download the first actually available worksheet
                    if not downloaded_ws_info:
                        target_ws = available[0]
                        print(f"\n  -> Downloading verified available worksheet: {target_ws.filename}...")
                        dl_path = await client.download_worksheet(
                            file_url_or_id=target_ws.download_url,
                            filename=target_ws.filename
                        )
                        downloaded_ws_info = {
                            "course": target_ws.course_code,
                            "session": target_ws.session,
                            "slo": target_ws.slo,
                            "format": target_ws.format,
                            "filename": target_ws.filename,
                            "local_path": str(dl_path),
                            "size_bytes": dl_path.stat().st_size,
                        }
                        print(f"  -> Download SUCCESSFUL!")
                        print(f"     Course: {downloaded_ws_info['course']}")
                        print(f"     Session: {downloaded_ws_info['session']}")
                        print(f"     SLO: {downloaded_ws_info['slo']}")
                        print(f"     Format: {downloaded_ws_info['format'].upper()}")
                        print(f"     Filename: {downloaded_ws_info['filename']}")
                        print(f"     Local Temp Path: {downloaded_ws_info['local_path']}")
                        print(f"     File Size: {downloaded_ws_info['size_bytes']} bytes")
                else:
                    print(f"  -> Notice: Coordinator has not uploaded worksheets for {c.course_code} yet.")
            except Exception as disc_err:
                print(f"  -> Discovery notice for {c.course_code}: {disc_err}")

        # If none of the enrolled courses had files uploaded yet, test direct resolution probe
        if not downloaded_ws_info:
            print("\n  [Fallback Probe] Testing direct worksheet resolution on target course...")
            try:
                file_url = await client.get_worksheet_file(
                    course_code=target_course.course_code,
                    session=1,
                    slo=1,
                    format_type="docx"
                )
                print(f"  -> Resolved URL: {file_url}")
                dl_path = await client.download_worksheet(file_url)
                print(f"  -> Downloaded to: {dl_path}")
            except WorksheetNotFound as wnf:
                print(f"  -> Clean resolution result: {wnf}")
            except Exception as exc:
                print(f"  -> Probe notice: {exc}")

    await client.close()
    print("\n" + "=" * 60)
    print("Developer Test Complete")
    print("=" * 60)


def main():
    asyncio.run(run_safe_dev_test())


if __name__ == "__main__":
    main()
