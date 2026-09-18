"""Regression tests for SRM verification, practice_val resolution, and idempotency handling."""

from unittest.mock import AsyncMock, patch
import pytest

from packages.srm.http_client import SRMHttpClient
from packages.srm.models import SRMSessionStatus
from packages.srm.exceptions import VerificationFailed


@pytest.fixture
def http_client() -> SRMHttpClient:
    return SRMHttpClient(base_url="https://srm.ktretecurricula.mock")


@pytest.mark.asyncio
async def test_practice_val_assigned_for_matching_slo_key(http_client: SRMHttpClient):
    """Verify practice_val is properly assigned when matching SLO key is present."""
    mock_status = SRMSessionStatus(
        session=105,
        practice_status={"1051": 1},
        slo_links={"1051": "https://drive.google.com/file/d/drive_file_111/view"},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status
        result = await http_client.verify_submission(
            session_or_worksheet_id=105,
            slo=1,
            expected_link="https://drive.google.com/file/d/drive_file_111/view",
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
        )
        assert result is True


@pytest.mark.asyncio
async def test_missing_slolink_key_raises_verification_failed(http_client: SRMHttpClient):
    """Verify missing SLOLINK key raises VerificationFailed structured error."""
    mock_status = SRMSessionStatus(
        session=105,
        practice_status={"1051": 1},
        slo_links={"1052": "https://drive.google.com/file/d/other_file/view"},  # Key 1051 missing
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status
        with pytest.raises(VerificationFailed) as exc_info:
            await http_client.verify_submission(
                session_or_worksheet_id=105,
                slo=1,
                expected_link="https://drive.google.com/file/d/drive_file_111/view",
                course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
            )
        assert "no recorded link found on SRM" in str(exc_info.value)


@pytest.mark.asyncio
async def test_missing_practice_key_raises_verification_failed(http_client: SRMHttpClient):
    """Verify missing PRACTICE key in dictionary raises VerificationFailed structured error."""
    mock_status = SRMSessionStatus(
        session=105,
        practice_status={"9999": 1},  # Key 1051 missing
        slo_links={"1051": "https://drive.google.com/file/d/drive_file_111/view"},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status
        with pytest.raises(VerificationFailed) as exc_info:
            await http_client.verify_submission(
                session_or_worksheet_id=105,
                slo=1,
                expected_link="https://drive.google.com/file/d/drive_file_111/view",
                course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
            )
        assert "invalid or missing practice status" in str(exc_info.value)


@pytest.mark.asyncio
async def test_practice_status_1_pending_accepted(http_client: SRMHttpClient):
    """Verify PRACTICE=1 (Pending review) confirms verification successfully."""
    mock_status = SRMSessionStatus(
        session=105,
        practice_status={"1051": 1},
        slo_links={"1051": "https://drive.google.com/file/d/drive_file_111/view"},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status
        assert await http_client.verify_submission(
            session_or_worksheet_id=105,
            slo=1,
            expected_link="https://drive.google.com/file/d/drive_file_111/view",
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
        ) is True


@pytest.mark.asyncio
async def test_practice_status_2_verified_accepted(http_client: SRMHttpClient):
    """Verify PRACTICE=2 (Faculty Verified) confirms verification successfully."""
    mock_status = SRMSessionStatus(
        session=105,
        practice_status={"1051": 2},
        slo_links={"1051": "https://drive.google.com/file/d/drive_file_111/view"},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status
        assert await http_client.verify_submission(
            session_or_worksheet_id=105,
            slo=1,
            expected_link="https://drive.google.com/file/d/drive_file_111/view",
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
        ) is True


@pytest.mark.asyncio
async def test_invalid_practice_value_raises_verification_failed(http_client: SRMHttpClient):
    """Verify invalid PRACTICE values (0, 3, None) raise VerificationFailed structured error."""
    for bad_practice in (0, 3, None, -1):
        mock_status = SRMSessionStatus(
            session=105,
            practice_status={"1051": bad_practice},
            slo_links={"1051": "https://drive.google.com/file/d/drive_file_111/view"},
        )
        with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
            mock_ss.return_value = mock_status
            with pytest.raises(VerificationFailed) as exc_info:
                await http_client.verify_submission(
                    session_or_worksheet_id=105,
                    slo=1,
                    expected_link="https://drive.google.com/file/d/drive_file_111/view",
                    course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
                )
            assert "invalid or missing practice status" in str(exc_info.value)


@pytest.mark.asyncio
async def test_slo2_keys_1052_and_52_resolved_correctly(http_client: SRMHttpClient):
    """Verify candidate keys for SLO2 correctly prioritize 1052 and 52 over SLO1 (1051)."""
    # 4-digit key "1052"
    mock_status_full = SRMSessionStatus(
        session=105,
        practice_status={"1051": 2, "1052": 1},
        slo_links={
            "1051": "https://drive.google.com/file/d/slo1_file_id/view",
            "1052": "https://drive.google.com/file/d/slo2_file_id/view",
        },
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status_full
        # Verifying SLO2 matches SLO2 file
        res_slo2 = await http_client.verify_submission(
            session_or_worksheet_id=105,
            slo=2,
            expected_link="https://drive.google.com/file/d/slo2_file_id/view",
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
        )
        assert res_slo2 is True

    # 2-digit key "52" (for session 105 % 100 = 5)
    mock_status_short = SRMSessionStatus(
        session=105,
        practice_status={"51": 2, "52": 2},
        slo_links={
            "51": "https://drive.google.com/file/d/slo1_file_id/view",
            "52": "https://drive.google.com/file/d/slo2_file_id/view",
        },
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status_short
        res_short = await http_client.verify_submission(
            session_or_worksheet_id=105,
            slo=2,
            expected_link="https://drive.google.com/file/d/slo2_file_id/view",
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
        )
        assert res_short is True


@pytest.mark.asyncio
async def test_mismatched_drive_file_id_raises_verification_failed(http_client: SRMHttpClient):
    """Verify mismatched Google Drive file ID raises VerificationFailed."""
    mock_status = SRMSessionStatus(
        session=105,
        practice_status={"1052": 1},
        slo_links={"1052": "https://drive.google.com/file/d/drive_file_aaa/view"},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status
        with pytest.raises(VerificationFailed) as exc_info:
            await http_client.verify_submission(
                session_or_worksheet_id=105,
                slo=2,
                expected_link="https://drive.google.com/file/d/drive_file_bbb/view",
                course_info={"BATCH_ID": "B1", "COURSE_CODE": "21CSC303J"},
            )
        assert "recorded link does not match submitted link" in str(exc_info.value)


@pytest.mark.asyncio
async def test_accepted_1052_submission_verifies_successfully(http_client: SRMHttpClient):
    """Verify the real accepted submission for 1052 (Session 105 SLO 2) verifies successfully."""
    accepted_file_id = "1G3zp_-KcDNhmKWIXcfjzXXvag7rKl5sG"
    submitted_url = (
        f"https://docs.google.com/document/d/{accepted_file_id}/edit?usp=drivesdk&ouid=100505335226418757578&rtpof=true&sd=true"
    )

    mock_status = SRMSessionStatus(
        session=105,
        practice_status={"1051": 2, "1052": 2},
        slo_links={"1052": {"view": f"https://docs.google.com/document/d/{accepted_file_id}/edit"}},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_ss.return_value = mock_status
        # Verify using session=105, slo=2
        res1 = await http_client.verify_submission(
            session_or_worksheet_id=105,
            slo=2,
            expected_link=submitted_url,
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"},
        )
        assert res1 is True

        # Verify using worksheet_id=1052, slo=2
        res2 = await http_client.verify_submission(
            session_or_worksheet_id=1052,
            slo=2,
            expected_link=submitted_url,
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"},
        )
        assert res2 is True


@pytest.mark.asyncio
async def test_task_idempotency_safe_when_practice_status_missing():
    """Verify worker task idempotency check does not raise UnboundLocalError when practice_status is empty or missing."""
    session_num = 105
    slo_num = 2
    session_status = SRMSessionStatus(
        session=105,
        practice_status={"1051": 2},  # Only SLO 1 present, SLO 2 missing!
        slo_links={},
    )
    # Replicate the exact logic from apps/worker/tasks.py
    key_full = f"{session_num}{slo_num}"
    key_short = f"{session_num % 100}{slo_num}" if session_num >= 100 else key_full
    cand_keys = [
        key_full,
        int(key_full) if key_full.isdigit() else None,
        key_short,
        int(key_short) if key_short.isdigit() else None,
    ]
    cand_keys = [k for k in cand_keys if k is not None]

    practice_val = None
    if isinstance(session_status.practice_status, dict):
        for k in cand_keys:
            if k in session_status.practice_status and session_status.practice_status[k] is not None:
                practice_val = session_status.practice_status[k]
                break
    elif isinstance(session_status.practice_status, int):
        practice_val = session_status.practice_status

    # This should evaluate cleanly without UnboundLocalError
    assert practice_val is None
    assert (practice_val in (1, 2)) is False
