"""Controlled verification test for SRM session 1021 submission and verification.

Simulates the exact real-world scenario encountered during live execution of Session 1021:
- Submitted Drive link shape: https://docs.google.com/document/d/<file_id>/edit?usp=drivesdk&ouid=<ouid>&rtpof=true&sd=true
- SRM getsessionstatus responses:
    Case 1: JSON dict wrapping {"view": "https://docs.google.com/document/d/<file_id>/edit", "download": "..."}
    Case 2: Canonical Drive URL https://drive.google.com/file/d/<file_id>/view with PRACTICE=1 (Pending)
    Case 3: Canonical Drive URL with PRACTICE=2 (Verified)
    Case 4: Unit-relative session key "21"
    Case 5: Strict rejection of mismatching Drive file ID
    Case 6: Strict rejection of absent/empty recorded link
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")

import pytest

from packages.srm.http_client import SRMHttpClient, canonicalize_submission_url, extract_google_drive_file_id
from packages.srm.models import SRMSessionStatus
from packages.srm.exceptions import VerificationFailed


@pytest.mark.asyncio
async def test_controlled_session_1021_verification():
    print("=" * 65)
    print("CONTROLLED SRM SESSION 1021 VERIFICATION TEST")
    print("=" * 65)

    client = SRMHttpClient()
    file_id = "1h5JqmjkXDTZrDtDZrnTsj2Va6mp27bfS"
    submitted_link = (
        f"https://docs.google.com/document/d/{file_id}/edit?usp=drivesdk&ouid=101921319167970497880&rtpof=true&sd=true"
    )
    course_info = {"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"}

    # 1. Component Extraction Verification
    extracted_id = extract_google_drive_file_id(submitted_link)
    assert extracted_id == file_id
    canonical_submitted = canonicalize_submission_url(submitted_link)
    assert canonical_submitted == f"https://drive.google.com/file/d/{file_id}/view"
    print(f"[Check 1] Extracted File ID    : {extracted_id}")
    print(f"[Check 1] Canonical Submitted  : {canonical_submitted}")

    # Case 1: Dict wrapping with query parameters stripped and /edit preserved
    status_case1 = SRMSessionStatus(
        session=102,
        practice_status={"1021": 1},
        slo_links={"1021": {"view": f"https://docs.google.com/document/d/{file_id}/edit", "download": f"https://drive.google.com/uc?id={file_id}"}},
    )
    with patch.object(client, "get_session_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = status_case1
        res1 = await client.verify_submission(
            session_or_worksheet_id=102,
            slo=1,
            expected_link=submitted_link,
            course_info=course_info,
        )
        assert res1 is True
        print("[Check 2] Case 1 (Dict with /edit stripped)       : PASS")

    # Case 2: Canonical Drive URL with PRACTICE=1 (Pending)
    status_case2 = SRMSessionStatus(
        session=102,
        practice_status={"1021": 1},
        slo_links={"1021": f"https://drive.google.com/file/d/{file_id}/view"},
    )
    with patch.object(client, "get_session_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = status_case2
        res2 = await client.verify_submission(
            session_or_worksheet_id=102,
            slo=1,
            expected_link=submitted_link,
            course_info=course_info,
        )
        assert res2 is True
        print("[Check 3] Case 2 (Drive /view URL, PRACTICE=1)     : PASS")

    # Case 3: Canonical Drive URL with PRACTICE=2 (Verified)
    status_case3 = SRMSessionStatus(
        session=102,
        practice_status={"1021": 2},
        slo_links={"1021": f"https://drive.google.com/file/d/{file_id}/view"},
    )
    with patch.object(client, "get_session_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = status_case3
        res3 = await client.verify_submission(
            session_or_worksheet_id=102,
            slo=1,
            expected_link=submitted_link,
            course_info=course_info,
        )
        assert res3 is True
        print("[Check 4] Case 3 (Drive /view URL, PRACTICE=2)     : PASS")

    # Case 4: Unit-relative session key "21"
    status_case4 = SRMSessionStatus(
        session=102,
        practice_status={"21": 1},
        slo_links={"21": {"view": f"https://drive.google.com/file/d/{file_id}/view"}},
    )
    with patch.object(client, "get_session_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = status_case4
        res4 = await client.verify_submission(
            session_or_worksheet_id=102,
            slo=1,
            expected_link=submitted_link,
            course_info=course_info,
        )
        assert res4 is True
        print("[Check 5] Case 4 (Unit-relative key '21')          : PASS")

    # Case 5: Strict rejection of mismatching Drive file ID
    wrong_file_id = "1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    status_case5 = SRMSessionStatus(
        session=102,
        practice_status={"1021": 1},
        slo_links={"1021": {"view": f"https://drive.google.com/file/d/{wrong_file_id}/view"}},
    )
    with patch.object(client, "get_session_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = status_case5
        with pytest.raises(VerificationFailed):
            await client.verify_submission(
                session_or_worksheet_id=102,
                slo=1,
                expected_link=submitted_link,
                course_info=course_info,
            )
        print("[Check 6] Case 5 (Strict rejection of wrong file)  : PASS")

    # Case 6: Strict rejection of empty/missing recorded link
    status_case6 = SRMSessionStatus(
        session=102,
        practice_status={"1021": 0},
        slo_links={},
    )
    with patch.object(client, "get_session_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = status_case6
        with pytest.raises(VerificationFailed):
            await client.verify_submission(
                session_or_worksheet_id=102,
                slo=1,
                expected_link=submitted_link,
                course_info=course_info,
            )
        print("[Check 7] Case 6 (Strict rejection of missing link): PASS")

    print("=" * 65)
    print("ALL CONTROLLED VERIFICATION CHECKS SUCCEEDED")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(test_controlled_session_1021_verification())
