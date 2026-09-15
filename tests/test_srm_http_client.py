import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import pytest

from packages.srm.http_client import SRMHttpClient
from packages.srm.models import (
    SRMCourse,
    SRMQuestionSet,
    SRMSessionStatus,
    SRMSubmissionResult,
    SRMWorksheetMetadata,
    SRMCourseStatus,
)
from packages.srm.exceptions import (
    AuthenticationFailed,
    WorksheetNotFound,
    DownloadFailed,
    SubmissionFailed,
    VerificationFailed,
    Unauthorized,
    InvalidSession,
    SRMConnectionError,
)


@pytest.fixture
def http_client():
    return SRMHttpClient(base_url="https://dld.srmist.edu.in", key="john", max_retries=2)


@pytest.mark.asyncio
async def test_authentication_success(http_client: SRMHttpClient):
    """Verify successful authentication stores JWT in-memory and sets user context."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 1,
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock_token_abc",
        "user": {
            "USER_ID": "RA2111003010001",
            "FULL_NAME": "Test Student",
            "DEPARTMENT": "CINTEL",
            "ROLE": "S",
        }
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        result = await http_client.authenticate({
            "username": "RA2111003010001",
            "password": "SecretPassword123"
        })

        assert result is True
        assert http_client.is_authenticated is True
        assert http_client._jwt_token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock_token_abc"
        assert http_client._user_id == "RA2111003010001"
        assert http_client._user_data["DEPARTMENT"] == "CINTEL"


@pytest.mark.asyncio
async def test_authentication_failure(http_client: SRMHttpClient):
    """Verify invalid credentials raise AuthenticationFailed."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 0,
        "msg": "Invalid Username or Password"
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        with pytest.raises(AuthenticationFailed) as exc_info:
            await http_client.authenticate({
                "username": "BAD_USER",
                "password": "WRONG_PASSWORD"
            })

        assert "Invalid Username or Password" in str(exc_info.value)
        assert http_client.is_authenticated is False
        assert http_client._jwt_token is None


@pytest.mark.asyncio
async def test_course_retrieval_and_semester_filtering(http_client: SRMHttpClient):
    """Verify get_courses parses course records and get_courses_by_semester filters correctly."""
    http_client._jwt_token = "mock_jwt"
    http_client._user_id = "RA2111003010001"

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 1,
        "courses": [
            {"COURSE_CODE": "21CSC301J", "COURSE_NAME": "OS", "SEMESTER": 3, "BATCH_ID": "B1", "DEPARTMENT": "CSE"},
            {"COURSE_CODE": "21CSC302J", "COURSE_NAME": "DBMS", "SEMESTER": 3, "BATCH_ID": "B1", "DEPARTMENT": "CSE"},
            {"COURSE_CODE": "21CSC401J", "COURSE_NAME": "AI", "SEMESTER": 4, "BATCH_ID": "B2", "DEPARTMENT": "CSE"},
        ]
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        all_courses = await http_client.get_courses()
        assert len(all_courses) == 3
        assert all_courses[0].course_code == "21CSC301J"
        assert all_courses[0].semester == 3

        sem3_courses = await http_client.get_courses_by_semester(3)
        assert len(sem3_courses) == 2
        assert {c.course_code for c in sem3_courses} == {"21CSC301J", "21CSC302J"}

        sem4_courses = await http_client.get_courses_by_semester(4)
        assert len(sem4_courses) == 1
        assert sem4_courses[0].course_code == "21CSC401J"


@pytest.mark.asyncio
async def test_question_retrieval(http_client: SRMHttpClient):
    """Verify get_questions parses MCQ, Short, and Long questions."""
    http_client._jwt_token = "mock_jwt"
    http_client._user_id = "RA2111003010001"

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 1,
        "mcq": [{"qid": 1, "q": "What is OS?"}],
        "sq": [{"qid": 2, "q": "Define thread"}],
        "lq": [{"qid": 3, "q": "Explain paging"}],
        "slo": {"name": "SLO 1"},
        "sp": {"plan": "Session 1"},
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        q_set = await http_client.get_questions(
            course_code="21CSC301J",
            batch_id="B1",
            session=1
        )
        assert isinstance(q_set, SRMQuestionSet)
        assert len(q_set.mcq) == 1
        assert len(q_set.sq) == 1
        assert len(q_set.lq) == 1
        assert q_set.session == 1


@pytest.mark.asyncio
async def test_session_status_retrieval(http_client: SRMHttpClient):
    """Verify get_session_status parses practice status and links."""
    http_client._jwt_token = "mock_jwt"
    http_client._user_id = "RA2111003010001"

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 1,
        "result": {
            "PRACTICE": {"11": 1},
            "SLOLINK": {"11": "https://drive.google.com/file/d/test1/view"}
        },
        "SKILLQ_SLO1": "1",
        "SKILLQ_SLO2": "0"
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        status = await http_client.get_session_status(
            course_info={"BATCH_ID": "B1"},
            session=1
        )
        assert isinstance(status, SRMSessionStatus)
        assert status.practice_status["11"] == 1
        assert status.slo_links["11"] == "https://drive.google.com/file/d/test1/view"


@pytest.mark.asyncio
async def test_worksheet_file_lookup_and_download(http_client: SRMHttpClient, tmp_path: Path):
    """Verify get_worksheet_file resolves path and download_worksheet retrieves document."""
    http_client._jwt_token = "mock_jwt"

    # 1. Lookup
    mock_lookup_resp = MagicMock(spec=httpx.Response)
    mock_lookup_resp.status_code = 200
    mock_lookup_resp.json.return_value = {
        "Status": 1,
        "result": {"path": "https://dld.srmist.edu.in/files/worksheet_1.docx"}
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_lookup_resp

        file_url = await http_client.get_worksheet_file(
            course_code="21CSC301J",
            filename="worksheet_session_1.docx"
        )
        assert file_url == "https://dld.srmist.edu.in/files/worksheet_1.docx"

    # 2. Download
    mock_dl_resp = MagicMock(spec=httpx.Response)
    mock_dl_resp.status_code = 200
    mock_dl_resp.content = b"PK\x03\x04Mock DOCX Binary Content"

    with patch.object(http_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_subclient = MagicMock(spec=httpx.AsyncClient)
        mock_subclient.get = AsyncMock(return_value=mock_dl_resp)
        mock_get_client.return_value = mock_subclient

        out_path = await http_client.download_worksheet(
            file_url_or_id=file_url,
            destination_dir=tmp_path,
            filename="worksheet_1.docx"
        )
        assert out_path.exists()
        assert out_path.read_bytes() == b"PK\x03\x04Mock DOCX Binary Content"


@pytest.mark.asyncio
async def test_worksheet_file_lookup_schema_derived(http_client: SRMHttpClient):
    """Verify get_worksheet_file derives real SRM schema (11.docx / 11.pdf) and paths."""
    http_client._jwt_token = "mock_jwt"

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 1,
        "result": {"path": "https://dld.srmist.edu.in/files/11.docx"}
    }

    # Test DOCX derivation
    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        file_url = await http_client.get_worksheet_file(
            course_code="21CSC301J",
            session=1,
            slo=1,
            format_type="docx"
        )
        assert file_url == "https://dld.srmist.edu.in/files/11.docx"
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert "getfile" in call_args[0][1]
        assert call_args[1]["json_data"]["filename"] == "11.docx"
        assert call_args[1]["json_data"]["path"] == "data/coordinator/21CSC301J/slp"

    # Test PDF derivation
    mock_resp.json.return_value = {
        "Status": 1,
        "result": {"path": "https://dld.srmist.edu.in/files/11.pdf"}
    }
    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        file_url = await http_client.get_worksheet_file(
            course_code="21CSC301J",
            session=1,
            slo=1,
            format_type="pdf"
        )
        assert file_url == "https://dld.srmist.edu.in/files/11.pdf"
        call_args = mock_req.call_args
        assert call_args[1]["json_data"]["filename"] == "11.pdf"
        assert call_args[1]["json_data"]["path"] == "data/coordinator/21CSC301J/slppdf"


@pytest.mark.asyncio
async def test_worksheet_file_lookup_direct_static_fallback(http_client: SRMHttpClient):
    """Verify fallback to direct static uploads URL when getfile returns Status 0."""
    http_client._jwt_token = "mock_jwt"

    mock_getfile_resp = MagicMock(spec=httpx.Response)
    mock_getfile_resp.status_code = 200
    mock_getfile_resp.json.return_value = {
        "Status": 0,
        "msg": "File not found!!!"
    }

    mock_head_resp = MagicMock(spec=httpx.Response)
    mock_head_resp.status_code = 200

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req, \
         patch.object(http_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_req.return_value = mock_getfile_resp
        mock_subclient = MagicMock(spec=httpx.AsyncClient)
        mock_subclient.head = AsyncMock(return_value=mock_head_resp)
        mock_get_client.return_value = mock_subclient

        file_url = await http_client.get_worksheet_file(
            course_code="21CSC301J",
            session=1,
            slo=1,
            format_type="docx"
        )
        expected_url = f"{http_client.questions_server_url}/uploads/data/coordinator/21CSC301J/slp/11.docx"
        assert file_url == expected_url


@pytest.mark.asyncio
async def test_worksheet_file_lookup_not_found(http_client: SRMHttpClient):
    """Verify WorksheetNotFound is raised when both getfile and static probe fail."""
    http_client._jwt_token = "mock_jwt"

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 0,
        "msg": "File not found on storage"
    }

    mock_head_resp = MagicMock(spec=httpx.Response)
    mock_head_resp.status_code = 404

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req, \
         patch.object(http_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_req.return_value = mock_resp
        mock_subclient = MagicMock(spec=httpx.AsyncClient)
        mock_subclient.head = AsyncMock(return_value=mock_head_resp)
        mock_get_client.return_value = mock_subclient

        with pytest.raises(WorksheetNotFound) as exc_info:
            await http_client.get_worksheet_file(
                course_code="21CSC301J",
                filename="nonexistent.docx"
            )
        assert "nonexistent.docx" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_course_status(http_client: SRMHttpClient):
    """Verify get_course_status parses course sessionCount, slp, and slppdf registers."""
    http_client._jwt_token = "mock_jwt"

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 1,
        "result": {
            "sessionCount": [
                {"_id": 1, "UNITNAME": "Unit 1 Intro", "SESSIONCOUNT": 12},
                {"_id": 2, "UNITNAME": "Unit 2 Adv", "SESSIONCOUNT": 10},
            ],
            "slp": [1011, 1012, 1021],
            "slppdf": [1011, 1012],
            "slpPractice": [1011, 1012],
            "assessment": [],
        }
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_resp

        status = await http_client.get_course_status("21CSC303J")
        assert status.course_code == "21CSC303J"
        assert len(status.session_count) == 2
        assert status.available_slp == [1011, 1012, 1021]
        assert status.available_slppdf == [1011, 1012]
        assert status.available_practice == [1011, 1012]


@pytest.mark.asyncio
async def test_discover_worksheets_full_course(http_client: SRMHttpClient):
    """Verify discover_worksheets returns complete structured metadata across course units."""
    http_client._jwt_token = "mock_jwt"

    mock_course_status = SRMCourseStatus(
        course_code="21CSC303J",
        session_count=[
            {"_id": 1, "UNITNAME": "Introduction", "SESSIONCOUNT": 2},
        ],
        available_slp=[1011, 1012],
        available_slppdf=[1011],
        available_practice=[1011],
    )

    with patch.object(http_client, "get_course_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = mock_course_status

        worksheets = await http_client.discover_worksheets("21CSC303J")
        # 1 unit * 2 sessions * 2 SLOs * 2 formats (docx + pdf) = 8 entries
        assert len(worksheets) == 8

        # Check DOCX available
        ws_1011_docx = next(w for w in worksheets if w.filename == "1011.docx")
        assert ws_1011_docx.is_available is True
        assert ws_1011_docx.course_code == "21CSC303J"
        assert ws_1011_docx.session == 101
        assert ws_1011_docx.slo == 1
        assert ws_1011_docx.unit == 1
        assert ws_1011_docx.session_no == 1
        assert ws_1011_docx.format == "docx"
        assert ws_1011_docx.storage_path == "data/coordinator/21CSC303J/slp"
        assert "uploads/data/coordinator/21CSC303J/slp/1011.docx" in ws_1011_docx.download_url

        # Check PDF available
        ws_1011_pdf = next(w for w in worksheets if w.filename == "1011.pdf")
        assert ws_1011_pdf.is_available is True
        assert ws_1011_pdf.format == "pdf"
        assert ws_1011_pdf.storage_path == "data/coordinator/21CSC303J/slppdf"

        # Check PDF not available (1012.pdf not in available_slppdf)
        ws_1012_pdf = next(w for w in worksheets if w.filename == "1012.pdf")
        assert ws_1012_pdf.is_available is False
        assert ws_1012_pdf.download_url is None


@pytest.mark.asyncio
async def test_discover_worksheets_filtered(http_client: SRMHttpClient):
    """Verify discover_worksheets supports filtering by session and format."""
    http_client._jwt_token = "mock_jwt"

    mock_course_status = SRMCourseStatus(
        course_code="21CSC303J",
        available_slp=[1011],
        available_slppdf=[],
    )

    with patch.object(http_client, "get_course_status", new_callable=AsyncMock) as mock_status:
        mock_status.return_value = mock_course_status

        worksheets = await http_client.discover_worksheets(
            course_code="21CSC303J",
            session=101,
            format_type="docx",
        )
        # Session 101 * 2 SLOs * 1 format = 2 entries
        assert len(worksheets) == 2
        assert all(w.format == "docx" for w in worksheets)
        assert [w.filename for w in worksheets] == ["1011.docx", "1012.docx"]
        assert worksheets[0].is_available is True
        assert worksheets[1].is_available is False


@pytest.mark.asyncio
async def test_discover_worksheets_with_practice_status(http_client: SRMHttpClient):
    """Verify discover_worksheets incorporates practice submission state."""
    http_client._jwt_token = "mock_jwt"

    mock_course_status = SRMCourseStatus(
        course_code="21CSC303J",
        available_slp=[1011],
    )
    mock_session_status = SRMSessionStatus(
        session=101,
        practice_status={"1011": 2, "1012": 1},
        slo_links={"1011": {"view": "https://drive.google.com/test_verified"}},
    )

    with patch.object(http_client, "get_course_status", new_callable=AsyncMock) as mock_cs, \
         patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_ss:
        mock_cs.return_value = mock_course_status
        mock_ss.return_value = mock_session_status

        worksheets = await http_client.discover_worksheets(
            course_code="21CSC303J",
            batch_id="BATCH_123",
            session=101,
            format_type="docx",
        )
        ws_1011 = next(w for w in worksheets if w.filename == "1011.docx")
        assert ws_1011.submission_status == "VERIFIED"
        assert ws_1011.submitted_link == "https://drive.google.com/test_verified"

        ws_1012 = next(w for w in worksheets if w.filename == "1012.docx")
        assert ws_1012.submission_status == "PENDING"


@pytest.mark.asyncio
async def test_submission_and_verification(http_client: SRMHttpClient):
    """Verify submit_worksheet_link issues UPDATE action and verify_submission confirms link."""
    http_client._jwt_token = "mock_jwt"
    http_client._user_id = "RA2111003010001"

    # 1. Submission
    mock_sub_resp = MagicMock(spec=httpx.Response)
    mock_sub_resp.status_code = 200
    mock_sub_resp.json.return_value = {
        "Status": 1,
        "msg": "Link Updated Successfully",
        "link": "https://drive.google.com/file/d/submitted_link/view"
    }

    with patch.object(http_client, "_request_with_retry", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_sub_resp

        result = await http_client.submit_worksheet_link(
            view_link="https://drive.google.com/file/d/submitted_link/view",
            download_link="https://drive.google.com/file/d/submitted_link/view",
            session=1,
            slo=1,
            course_code="21CSC301J",
            course_name="Operating Systems",
            batch_id="B1",
        )
        assert result.success is True
        assert result.returned_link == "https://drive.google.com/file/d/submitted_link/view"

    # 2. Verification (exact matching)
    mock_status = SRMSessionStatus(
        session=1,
        practice_status={"11": 1},
        slo_links={"11": "https://drive.google.com/file/d/submitted_link_1234567890/view"},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_get_status:
        mock_get_status.return_value = mock_status

        verified = await http_client.verify_submission(
            session_or_worksheet_id=1,
            slo=1,
            expected_link="https://drive.google.com/file/d/submitted_link_1234567890/view",
            course_info={"BATCH_ID": "B1"}
        )
        assert verified is True

        # Verification with mismatching expected link should fail
        with pytest.raises(VerificationFailed):
            await http_client.verify_submission(
                session_or_worksheet_id=1,
                slo=1,
                expected_link="https://drive.google.com/file/d/DIFFERENT_LINK_0987654321/view",
                course_info={"BATCH_ID": "B1"}
            )


@pytest.mark.asyncio
async def test_canonical_verification_scenarios(http_client: SRMHttpClient):
    """Verify verify_submission handles real SRM normalizations: dict wrapping, /edit vs /view, query stripping."""
    drive_file_id = "1h5JqmjkXDTZrDtDZrnTsj2Va6mp27bfS"
    submitted_full_url = (
        f"https://docs.google.com/document/d/{drive_file_id}/edit?usp=drivesdk&ouid=101921319167970497880&rtpof=true&sd=true"
    )

    # Scenario A: SRM returns dict {"view": "...", "download": "..."} with /edit stripped
    mock_status_dict = SRMSessionStatus(
        session=102,
        practice_status={"1021": 1},
        slo_links={"1021": {"view": f"https://docs.google.com/document/d/{drive_file_id}/edit", "download": ""}},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_get_status:
        mock_get_status.return_value = mock_status_dict
        verified = await http_client.verify_submission(
            session_or_worksheet_id=102,
            slo=1,
            expected_link=submitted_full_url,
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"},
        )
        assert verified is True

    # Scenario B: SRM normalizes to canonical drive.google.com/file/d/.../view
    mock_status_canonical = SRMSessionStatus(
        session=102,
        practice_status={"1021": 2},
        slo_links={"1021": f"https://drive.google.com/file/d/{drive_file_id}/view"},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_get_status:
        mock_get_status.return_value = mock_status_canonical
        verified = await http_client.verify_submission(
            session_or_worksheet_id=102,
            slo=1,
            expected_link=submitted_full_url,
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"},
        )
        assert verified is True

    # Scenario C: Key in SLOLINK is 2-digit "21" rather than 4-digit "1021"
    mock_status_short_key = SRMSessionStatus(
        session=102,
        practice_status={"21": 1},
        slo_links={"21": {"view": f"https://drive.google.com/file/d/{drive_file_id}/view"}},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_get_status:
        mock_get_status.return_value = mock_status_short_key
        verified = await http_client.verify_submission(
            session_or_worksheet_id=102,
            slo=1,
            expected_link=submitted_full_url,
            course_info={"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"},
        )
        assert verified is True

    # Scenario D: Rejection on different Google Drive file ID
    mock_status_wrong_id = SRMSessionStatus(
        session=102,
        practice_status={"1021": 1},
        slo_links={"1021": {"view": "https://drive.google.com/file/d/WRONG_DIFFERENT_FILE_ID_12345/view"}},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_get_status:
        mock_get_status.return_value = mock_status_wrong_id
        with pytest.raises(VerificationFailed) as exc_info:
            await http_client.verify_submission(
                session_or_worksheet_id=102,
                slo=1,
                expected_link=submitted_full_url,
                course_info={"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"},
            )
        assert "recorded link does not match" in str(exc_info.value)

    # Scenario E: Rejection when no link is recorded on SRM
    mock_status_no_link = SRMSessionStatus(
        session=102,
        practice_status={"1021": 0},
        slo_links={},
    )
    with patch.object(http_client, "get_session_status", new_callable=AsyncMock) as mock_get_status:
        mock_get_status.return_value = mock_status_no_link
        with pytest.raises(VerificationFailed) as exc_info:
            await http_client.verify_submission(
                session_or_worksheet_id=102,
                slo=1,
                expected_link=submitted_full_url,
                course_info={"BATCH_ID": "B1", "COURSE_CODE": "21LEM202T"},
            )
        assert "no recorded link found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_retry_behavior_on_transient_error(http_client: SRMHttpClient):
    """Verify HTTP client automatically retries transient 503 error before succeeding."""
    http_client._client = MagicMock(spec=httpx.AsyncClient)
    http_client._client.is_closed = False

    resp_503 = MagicMock(spec=httpx.Response)
    resp_503.status_code = 503

    resp_200 = MagicMock(spec=httpx.Response)
    resp_200.status_code = 200
    resp_200.json.return_value = {"Status": 1}

    http_client._client.request = AsyncMock(side_effect=[resp_503, resp_200])

    response = await http_client._request_with_retry(
        "POST", "https://dld.srmist.edu.in/ktretecurricula/server/curricula/checkstatus"
    )
    assert response.status_code == 200
    assert http_client._client.request.call_count == 2


@pytest.mark.asyncio
async def test_sensitive_data_redaction_in_logging(http_client: SRMHttpClient, caplog):
    """Verify that credentials, passwords, and JWT tokens are NEVER output to loggers."""
    caplog.set_level(logging.INFO)

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "Status": 1,
        "token": "SECRET_JWT_TOKEN_NEVER_LOG",
        "user": {"USER_ID": "RA12345", "FULL_NAME": "Confidential Student"}
    }

    with patch.object(http_client, "_get_client", new_callable=AsyncMock) as mock_get_client:
        mock_subclient = MagicMock(spec=httpx.AsyncClient)
        mock_subclient.request = AsyncMock(return_value=mock_resp)
        mock_get_client.return_value = mock_subclient

        await http_client.authenticate({
            "username": "RA12345",
            "password": "SUPER_SECRET_PASSWORD"
        })

        all_logs = " ".join(record.message for record in caplog.records)
        assert "SUPER_SECRET_PASSWORD" not in all_logs
        assert "SECRET_JWT_TOKEN_NEVER_LOG" not in all_logs


@pytest.mark.asyncio
async def test_retry_logging_format(http_client: SRMHttpClient, caplog):
    """Verify retry logging formats attempts cleanly as Attempt X/total and never logs Attempt 4/3."""
    caplog.set_level(logging.WARNING)
    http_client.max_retries = 3

    http_client._client = MagicMock(spec=httpx.AsyncClient)
    http_client._client.is_closed = False

    # Force all 4 attempts (1 initial + 3 retries) to fail with connection timeout
    http_client._client.request = AsyncMock(side_effect=httpx.ConnectTimeout("Connection timed out"))

    with pytest.raises(SRMConnectionError):
        await http_client._request_with_retry("POST", "https://dld.srmist.edu.in/curricula/test")

    log_messages = [record.message for record in caplog.records]
    # Check that it logged Attempt 1/4, 2/4, 3/4, 4/4
    assert any("Attempt 1/4" in msg for msg in log_messages)
    assert any("Attempt 2/4" in msg for msg in log_messages)
    assert any("Attempt 3/4" in msg for msg in log_messages)
    assert any("Attempt 4/4" in msg for msg in log_messages)
    # Check that it NEVER logged "Attempt 4/3"
    assert not any("Attempt 4/3" in msg for msg in log_messages)
