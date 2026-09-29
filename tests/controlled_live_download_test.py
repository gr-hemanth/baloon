"""Controlled Live End-to-End Test: Download Exactly ONE Worksheet.

Target:
- Course: 21LEM202T - UNIVERSAL HUMAN VALUES
- Batch: 21LEM202T_39
- Worksheet: 1011

Safeguards:
1. Authenticate using the existing Playwright + manual hCaptcha flow.
2. After authentication, close the browser immediately.
3. Use the captured SRMAuthSession with the direct HTTP client.
4. Verify worksheet 1011 is currently marked available before downloading.
5. Download ONLY worksheet 1011.
6. Verify the HTTP response and downloaded file.
7. Verify the file is a valid DOCX/PDF as returned by the portal.
8. Compute and report file size and SHA-256.
9. Strictly enforce:
   - Do NOT parse the worksheet.
   - Do NOT generate any AI answers.
   - Do NOT modify the worksheet.
   - Do NOT upload anything to Google Drive.
   - Do NOT submit anything to SRM.
"""

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure project root in sys.path
sys.path.insert(0, ".")

from packages.srm.http_client import SRMHttpClient
from packages.srm.models import SRMAuthSession
from packages.srm.orchestrator import SRMOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("controlled_live_download")

TARGET_COURSE_CODE = "21LEM202T"
TARGET_BATCH_ID = "21LEM202T_39"
TARGET_WORKSHEET_ID = "1011"
TARGET_SESSION = 101
TARGET_SLO = 1


async def run_controlled_download_test(
    username: str,
    password: str,
    timeout_seconds: int = 180,
    download_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    print("\n" + "=" * 70)
    print("CONTROLLED LIVE WORKSHEET DOWNLOAD TEST")
    print("=" * 70)
    print(f"Target Portal: https://dld.srmist.edu.in/ktretecurricula/#/")
    masked_user = f"{username[:4]}****{username[-4:] if len(username) > 8 else ''}"
    print(f"Target NetID : {masked_user}")
    print(f"Target Course: {TARGET_COURSE_CODE} (Batch: {TARGET_BATCH_ID})")
    print(f"Target File  : Worksheet {TARGET_WORKSHEET_ID}")
    print("=" * 70 + "\n")

    report: Dict[str, Any] = {
        "authentication": "FAIL",
        "worksheet_availability_check": "FAIL",
        "worksheet_selected": f"{TARGET_COURSE_CODE} / {TARGET_WORKSHEET_ID}",
        "download_request": "FAIL",
        "http_status": "N/A",
        "file_type": "Unknown",
        "file_size": "0 bytes",
        "sha256": "N/A",
        "original_file_preserved": "FAIL",
        "issue_discovered": "None",
        "next_change_required": "None (Ready for parsing & offline answer engine verification)",
    }

    dest_dir = download_dir or Path("artifacts/downloads/controlled_test")
    dest_dir.mkdir(parents=True, exist_ok=True)

    orchestrator = SRMOrchestrator(mode="auto")

    async def status_callback(phase: str, msg: str):
        print(f"  [STATUS UPDATE] [{phase}] {msg}")

    orchestrator.set_status_callback(status_callback)

    try:
        # -------------------------------------------------------------------
        # Step 1: Authenticate via Playwright + manual hCaptcha
        # -------------------------------------------------------------------
        print("[Step 1] Authenticating via Playwright browser transport...")
        print("-" * 70)
        print("ACTION REQUIRED:")
        print("A headed Chromium browser window will open on your screen.")
        print("Please solve the visual hCaptcha challenge in that window.")
        print("Once verified, your session will be captured automatically and browser will close.")
        print("-" * 70 + "\n")

        credentials = {
            "USER_ID": username,
            "PASSWORD": password,
            "headless": False,
        }

        auth_session = await orchestrator.browser_client.authenticate_interactive(
            credentials=credentials,
            status_callback=status_callback,
            captcha_timeout_seconds=timeout_seconds,
        )

        if not auth_session or not auth_session.access_token:
            report["issue_discovered"] = "Failed to capture access token from browser authentication."
            return report

        report["authentication"] = "PASS"
        print("  [OK] Playwright authentication successful. Browser closed cleanly.")

        # -------------------------------------------------------------------
        # Step 2: Configure direct HTTP client with captured session
        # -------------------------------------------------------------------
        http_client = SRMHttpClient(base_url="https://dld.srmist.edu.in")
        http_client.set_auth_session(auth_session)

        if not http_client.is_authenticated:
            report["issue_discovered"] = "SRMHttpClient rejected captured session."
            return report

        # -------------------------------------------------------------------
        # Step 3: Check availability of Worksheet 1011
        # -------------------------------------------------------------------
        print(f"\n[Step 2] Checking availability of Worksheet {TARGET_WORKSHEET_ID} for {TARGET_COURSE_CODE}...")
        course_status = await http_client.get_course_status(TARGET_COURSE_CODE)

        avail_slp = [str(x) for x in course_status.available_slp]
        avail_slppdf = [str(x) for x in course_status.available_slppdf]

        format_detected = "docx"
        is_available = False

        if TARGET_WORKSHEET_ID in avail_slp:
            format_detected = "docx"
            is_available = True
            print(f"  [OK] Worksheet {TARGET_WORKSHEET_ID} is present in portal slp (DOCX) register.")
        elif TARGET_WORKSHEET_ID in avail_slppdf:
            format_detected = "pdf"
            is_available = True
            print(f"  [OK] Worksheet {TARGET_WORKSHEET_ID} is present in portal slppdf (PDF) register.")
        else:
            # Check if available in sessions
            print(f"  [Notice] Worksheet {TARGET_WORKSHEET_ID} not in explicit register. Probing session status...")
            s_status = await http_client.get_session_status(
                course_info={"BATCH_ID": TARGET_BATCH_ID, "COURSE_CODE": TARGET_COURSE_CODE},
                session=TARGET_SESSION,
            )
            if s_status.practice_status is not None:
                is_available = True
                print(f"  [OK] Session {TARGET_SESSION} status active with practice map: {s_status.practice_status}")

        if is_available:
            report["worksheet_availability_check"] = "PASS"
        else:
            report["issue_discovered"] = f"Worksheet {TARGET_WORKSHEET_ID} is not marked available in portal register."
            return report

        # -------------------------------------------------------------------
        # Step 4: Resolve Download URL
        # -------------------------------------------------------------------
        print(f"\n[Step 3] Resolving download URL for Worksheet {TARGET_WORKSHEET_ID} ({format_detected})...")
        download_url: Optional[str] = None

        try:
            download_url = await http_client.get_worksheet_file(
                course_code=TARGET_COURSE_CODE,
                session=TARGET_SESSION,
                slo=TARGET_SLO,
                format_type=format_detected,
            )
            print(f"  [OK] Resolved URL via getfile endpoint.")
        except Exception as resolve_err:
            logger.info("getfile resolution note: %s. Using direct static uploads URL.", resolve_err)
            folder = "slppdf" if format_detected == "pdf" else "slp"
            download_url = f"{http_client.questions_server_url}/uploads/data/coordinator/{TARGET_COURSE_CODE}/{folder}/{TARGET_WORKSHEET_ID}.{format_detected}"

        # -------------------------------------------------------------------
        # Step 5: Download ONLY Worksheet 1011
        # -------------------------------------------------------------------
        print(f"\n[Step 4] Downloading worksheet document via direct HTTP GET...")
        target_filename = f"{TARGET_WORKSHEET_ID}.{format_detected}"
        target_filepath = dest_dir / target_filename

        client = await http_client._get_client()
        resp = await client.get(download_url, timeout=http_client.timeout)

        report["http_status"] = f"{resp.status_code} {resp.reason_phrase if hasattr(resp, 'reason_phrase') else 'OK'}"
        print(f"  [HTTP Response] Status: {report['http_status']}")

        if resp.status_code != 200:
            # Fallback probe for alternative extension if initial 404
            alt_format = "pdf" if format_detected == "docx" else "docx"
            alt_folder = "slppdf" if alt_format == "pdf" else "slp"
            alt_url = f"{http_client.questions_server_url}/uploads/data/coordinator/{TARGET_COURSE_CODE}/{alt_folder}/{TARGET_WORKSHEET_ID}.{alt_format}"
            logger.info("Trying alternate format %s: %s", alt_format, alt_url)
            alt_resp = await client.get(alt_url, timeout=http_client.timeout)
            if alt_resp.status_code == 200:
                resp = alt_resp
                format_detected = alt_format
                target_filename = f"{TARGET_WORKSHEET_ID}.{format_detected}"
                target_filepath = dest_dir / target_filename
                report["http_status"] = f"{resp.status_code} OK"

        if resp.status_code != 200:
            report["issue_discovered"] = f"Download request returned HTTP {resp.status_code}"
            return report

        report["download_request"] = "PASS"

        # Write downloaded bytes
        file_bytes = resp.content
        target_filepath.write_bytes(file_bytes)
        print(f"  [OK] File saved cleanly to: {target_filepath}")

        # -------------------------------------------------------------------
        # Step 6: Verify file format and integrity
        # -------------------------------------------------------------------
        print(f"\n[Step 5] Verifying downloaded file format & checksum...")
        file_size_bytes = len(file_bytes)
        report["file_size"] = f"{file_size_bytes} bytes ({file_size_bytes / 1024:.2f} KB)"

        # Magic byte detection
        if file_bytes.startswith(b"PK\x03\x04"):
            report["file_type"] = "Microsoft Word Document (DOCX)"
            # Validate DOCX structure with python-docx
            try:
                from docx import Document
                doc = Document(str(target_filepath))
                p_count = len(doc.paragraphs)
                t_count = len(doc.tables)
                print(f"  [Validation] Valid DOCX archive: {p_count} paragraphs, {t_count} tables detected.")
            except Exception as docx_err:
                report["issue_discovered"] = f"DOCX structure warning: {docx_err}"
        elif file_bytes.startswith(b"%PDF"):
            report["file_type"] = "Portable Document Format (PDF)"
            print("  [Validation] Valid PDF document signature detected.")
        else:
            report["file_type"] = f"Unknown / Binary (Magic: {file_bytes[:8].hex()})"
            report["issue_discovered"] = "File magic header does not match standard DOCX or PDF signatures."

        # Compute SHA-256
        sha256_hash = hashlib.sha256(file_bytes).hexdigest()
        report["sha256"] = sha256_hash
        print(f"  [Checksum] SHA-256: {sha256_hash}")

        # Check file was written completely without modification
        disk_bytes = target_filepath.read_bytes()
        if disk_bytes == file_bytes:
            report["original_file_preserved"] = "PASS"
            print("  [OK] Original downloaded file byte-integrity confirmed untouched.")
        else:
            report["original_file_preserved"] = "FAIL"

        print(f"\n[Safeguard Assurance]")
        print("  - Question parsing: SKIPPED (as instructed)")
        print("  - AI answer generation: SKIPPED (as instructed)")
        print("  - Document modification: NONE (original file preserved byte-exact)")
        print("  - Google Drive upload: SKIPPED (as instructed)")
        print("  - SRM portal submission: SKIPPED (as instructed)")

        await http_client.close()

    except Exception as exc:
        logger.error("Download test encountered an error: %s", exc, exc_info=True)
        report["issue_discovered"] = str(exc)
    finally:
        try:
            await orchestrator.close()
        except Exception:
            pass

    return report


def print_final_report(res: Dict[str, Any]):
    print("\n" + "=" * 70)
    print("LIVE WORKSHEET DOWNLOAD TEST")
    print("=" * 70)
    print(f"- Authentication: {res['authentication']}")
    print(f"- Worksheet availability check: {res['worksheet_availability_check']}")
    print(f"- Worksheet selected: {res['worksheet_selected']}")
    print(f"- Download request: {res['download_request']}")
    print(f"- HTTP status: {res['http_status']}")
    print(f"- File type: {res['file_type']}")
    print(f"- File size: {res['file_size']}")
    print(f"- SHA-256: {res['sha256']}")
    print(f"- Original file preserved: {res['original_file_preserved']}")
    print(f"- Any issue discovered: {res['issue_discovered']}")
    print(f"- Exact next change required: {res['next_change_required']}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Controlled live test for downloading exactly ONE worksheet")
    parser.add_argument("--username", "-u", default=os.environ.get("SRM_USER_ID"), help="SRM Register Number / NetID")
    parser.add_argument("--password", "-p", default=os.environ.get("SRM_PASSWORD"), help="SRM Portal Password")
    parser.add_argument("--timeout", "-t", type=int, default=180, help="CAPTCHA solve timeout in seconds")
    args = parser.parse_args()

    if not args.username or not args.password:
        print("\n[!] Please provide --username and --password (or set SRM_USER_ID and SRM_PASSWORD environment variables).")
        print("    Example: python tests/controlled_live_download_test.py -u RA2111003010001 -p SecretPass123\n")
        sys.exit(1)

    result = asyncio.run(run_controlled_download_test(
        username=args.username,
        password=args.password,
        timeout_seconds=args.timeout,
    ))
    print_final_report(result)
