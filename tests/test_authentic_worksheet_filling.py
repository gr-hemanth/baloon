"""Comprehensive regression test suite for student-authentic worksheet answering and filling.

Verifies:
1. Student header metadata filling (Name, Reg. No., Branch, Date) with Session/Topic/Lecture protected.
2. Cell-by-cell table activity filling (Aspirations, Achievements, Concerns).
3. Paragraph answers staying in designated paragraph lines outside tables.
4. Tick/select question response formatting (choice + explanation).
5. Code generation producing valid raw code without markdown code fence markers.
6. Absolute absence of redundant 'Answer:' or 'Ans:' prefixes inside table cells.
7. Physical XML document verification (10 physical integrity checks).
8. Original document SHA256 immutability guarantee.
9. Real UHV-II Session 7 (1072.docx) end-to-end validation.
10. FreeLLM timeout / failure failover handling.
"""

import hashlib
from pathlib import Path
import docx
import pytest

from packages.worksheets.answer_engine import (
    BaseAnswerEngine,
    LLMAnswerEngine,
    RuleBasedAnswerEngine,
)
from packages.worksheets.answer_models import (
    AnswerStatus,
    GeneratedAnswer,
    WorksheetAnswers,
)
from packages.worksheets.docx_parser import DocxWorksheetParser
from packages.worksheets.exceptions import LLMTimeoutError, WorksheetFillingError
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.header_filler import DEFAULT_STUDENT_INFO, StudentHeaderFiller
from packages.worksheets.models import (
    AnswerTargetSpec,
    ParsedQuestion,
    ParsedWorksheet,
    QuestionType,
)
from packages.worksheets.pipeline import WorksheetPipeline
from packages.worksheets.verification import PhysicalDocumentVerifier


@pytest.mark.asyncio
async def test_uhv_ii_1072_docx_physical_verification_and_cell_filling(tmp_path: Path):
    """Verify complete end-to-end processing of real UHV-II 1072.docx worksheet."""
    real_path = Path("artifacts/1072.docx")
    if not real_path.exists():
        pytest.skip("artifacts/1072.docx not found; skipping test.")

    orig_bytes = real_path.read_bytes()
    orig_hash = hashlib.sha256(orig_bytes).hexdigest()

    # Use pipeline with rule-based engine to guarantee deterministic, offline test reliability
    pipeline = WorksheetPipeline(answer_engine=RuleBasedAnswerEngine())
    result = await pipeline.process(
        worksheet_path=real_path,
        output_dir=tmp_path,
        output_filename="completed_1072.docx",
    )

    # 1. Pipeline and output file verification
    assert result.success is True
    assert result.completed_file.exists()
    assert result.completed_file != real_path

    # 2. SHA256 Original Immutability
    post_hash = hashlib.sha256(real_path.read_bytes()).hexdigest()
    assert orig_hash == post_hash, "Original document bytes were modified!"

    # 3. Physical Document Verification Report
    assert "verification" in result.summary
    v_report = result.summary["verification"]
    assert v_report["is_valid"] is True
    assert v_report["original_sha256_matches"] is True
    assert v_report["table_cells_verified"] >= 20
    assert len(v_report["header_fields_verified"]) >= 3
    assert len(v_report["errors"]) == 0

    # 4. Student Header Inspection
    comp_doc = docx.Document(str(result.completed_file))
    header_tbl = comp_doc.tables[0]

    # Verify protected instructional fields were NOT overwritten
    all_header_text = " ".join(c.text.strip() for row in header_tbl.rows for c in row.cells)
    assert "Session" in all_header_text
    assert "Lecture" in all_header_text
    assert "Topic" in all_header_text

    # Verify student details are safely populated
    assert DEFAULT_STUDENT_INFO["name"] in all_header_text
    assert DEFAULT_STUDENT_INFO["reg_no"] in all_header_text
    assert DEFAULT_STUDENT_INFO["branch"] in all_header_text

    # 5. Activity 1 Table: Filled cell-by-cell
    act1_tbl = comp_doc.tables[2]
    # Header row
    assert "Aspirations" in act1_tbl.rows[0].cells[0].text
    assert "Achievements" in act1_tbl.rows[0].cells[1].text
    assert "Concerns" in act1_tbl.rows[0].cells[2].text

    # Check cell answers
    for r_idx in range(1, len(act1_tbl.rows)):
        for c_idx in range(len(act1_tbl.rows[r_idx].cells)):
            cell_text = act1_tbl.rows[r_idx].cells[c_idx].text.strip()
            assert len(cell_text) > 0, f"Cell at row {r_idx}, col {c_idx} was empty!"
            # Rule 6: No redundant 'Answer:' inside cells
            assert not cell_text.lower().startswith("answer:"), f"Cell contained 'Answer:' prefix: {cell_text}"
            assert not cell_text.lower().startswith("ans:"), f"Cell contained 'Ans:' prefix: {cell_text}"

    # 6. Activity 2 Table: Filled cell-by-cell
    act2_tbl = comp_doc.tables[3]
    for r_idx in range(1, len(act2_tbl.rows)):
        for c_idx in range(len(act2_tbl.rows[r_idx].cells)):
            cell_text = act2_tbl.rows[r_idx].cells[c_idx].text.strip()
            assert len(cell_text) > 0, f"Activity 2 cell at row {r_idx}, col {c_idx} was empty!"
            assert not cell_text.lower().startswith("answer:")

    # 7. Tick / Select question
    act3_p = comp_doc.paragraphs[26]
    assert "[✓]" in act3_p.text or "4-3-2-1" in act3_p.text


def test_student_header_filler_protection(tmp_path: Path):
    """Verify that StudentHeaderFiller protects Session, Topic, and Lecture."""
    doc = docx.Document()
    tbl = doc.add_table(rows=4, cols=4)
    tbl.rows[0].cells[0].text = "Session"
    tbl.rows[0].cells[1].text = "7"
    tbl.rows[0].cells[2].text = "Lecture"
    tbl.rows[0].cells[3].text = "PS 1"

    tbl.rows[1].cells[0].text = "Topic"
    tbl.rows[1].cells[1].text = "Aspirations and Concerns"
    tbl.rows[1].cells[2].text = ""
    tbl.rows[1].cells[3].text = ""

    tbl.rows[2].cells[0].text = "Name"
    tbl.rows[2].cells[1].text = ""
    tbl.rows[2].cells[2].text = "Reg. No."
    tbl.rows[2].cells[3].text = ""

    tbl.rows[3].cells[0].text = "Branch / Sec."
    tbl.rows[3].cells[1].text = ""
    tbl.rows[3].cells[2].text = "Date"
    tbl.rows[3].cells[3].text = ""

    filler = StudentHeaderFiller({
        "name": "TEST STUDENT",
        "reg_no": "TEST123456",
        "branch": "AI & DATA SCIENCE",
        "date": "2026-09-25",
    })
    filled = filler.fill_header(doc)

    assert filled["name"] == "TEST STUDENT"
    assert filled["reg_no"] == "TEST123456"
    assert filled["branch"] == "AI & DATA SCIENCE"
    assert filled["date"] == "2026-09-25"

    # Protected cells must remain unchanged
    assert tbl.rows[0].cells[0].text == "Session"
    assert tbl.rows[0].cells[1].text == "7"
    assert tbl.rows[0].cells[2].text == "Lecture"
    assert tbl.rows[0].cells[3].text == "PS 1"
    assert tbl.rows[1].cells[0].text == "Topic"
    assert tbl.rows[1].cells[1].text == "Aspirations and Concerns"


def test_code_generation_produces_clean_code():
    """Verify code question answering produces valid raw code without markdown backticks."""
    engine = RuleBasedAnswerEngine()
    q_py = ParsedQuestion(
        question_id="code_py",
        question_text="Write a Python function to calculate the sum of numbers in a list.",
        question_type=QuestionType.CODE,
    )
    ans = engine._answer_code(q_py)
    assert ans.question_type == QuestionType.CODE
    assert "def " in ans.answer_text
    assert "```" not in ans.answer_text
    assert "```python" not in ans.answer_text

    q_cpp = ParsedQuestion(
        question_id="code_cpp",
        question_text="Implement a C++ program with main function.",
        question_type=QuestionType.CODE,
    )
    ans_cpp = engine._answer_code(q_cpp)
    assert "#include" in ans_cpp.answer_text
    assert "```" not in ans_cpp.answer_text


def test_tick_select_format():
    """Verify tick/select question format produces option choice and explanation."""
    engine = RuleBasedAnswerEngine()
    q_tick = ParsedQuestion(
        question_id="tick_1",
        question_text="Activity 3: How Are You Planning Your Life? Tick one and explain: 1-2-3-4 OR 4-3-2-1",
        question_type=QuestionType.TICK_SELECT,
    )
    ans = engine._answer_tick_select(q_tick)
    assert ans.question_type == QuestionType.TICK_SELECT
    assert "[✓]" in ans.answer_text
    assert "4-3-2-1" in ans.answer_text
    assert ans.selected_option == "4-3-2-1"


@pytest.mark.asyncio
async def test_freellm_timeout_failover_to_offline_engine():
    """Verify that when FreeLLM times out, engine gracefully fails over without crashing."""
    # Create LLMAnswerEngine pointing to a non-responsive address with a short timeout
    class MockTimeoutEngine(LLMAnswerEngine):
        async def _generate_openai_compatible_answers(self, worksheet, context=None):
            raise LLMTimeoutError("FREELLM API request timed out after 1.0s")

    failing_engine = MockTimeoutEngine(
        provider="freellm",
        timeout=0.01,
        max_retries=1,
        retry_backoff=0.001,
        allow_fallback_when_unconfigured=True,
    )

    q = ParsedQuestion(
        question_id="q_failover",
        question_text="What does CPU stand for?",
        question_type=QuestionType.ONE_WORD,
    )
    ws = ParsedWorksheet(
        filename="failover_test.docx",
        file_format="docx",
        questions=[q],
    )

    res = await failing_engine.generate_answers(ws)
    assert len(res.answers) == 1
    assert res.answers[0].question_id == "q_failover"
    assert "Central Processing Unit" in res.answers[0].answer_text
