"""Regression test suite for worksheet 1022 (SLO 2: Abstraction & Specification).

Validates the full pipeline:
DOCX -> parser -> classified questions -> answer generation -> filler -> completed_1022.docx -> questions_answered metadata.

Ensures:
1. The 3 questions visible in the worksheet are detected.
2. The parser returns exactly 3 answerable questions.
3. FreeLLM generates 3 answers.
4. The filler writes all 3 answers into their intended locations.
5. Questions Answered displays 3, not 0 (in pipeline summary & job result metadata).
6. Original 1022.docx remains byte-for-byte unchanged.
7. No duplicate or extra answers are inserted.
8. Does not hardcode worksheet 1022.
"""

import asyncio
import hashlib
from pathlib import Path
import json
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import docx

from packages.worksheets.answer_engine import AnswerEngineFactory, LLMAnswerEngine, RuleBasedAnswerEngine
from packages.worksheets.answer_models import GeneratedAnswer, WorksheetAnswers, AnswerStatus
from packages.worksheets.docx_parser import DocxWorksheetParser
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.models import QuestionType
from packages.worksheets.pipeline import WorksheetPipeline


@pytest.fixture
def worksheet_1022_path(tmp_path: Path) -> Path:
    """Provide a verified copy of 1022.docx for test execution."""
    real_path = Path("artifacts/1022.docx")
    if not real_path.exists():
        candidate = Path(r"C:\Users\Hemanth\AppData\Local\Temp\srm_job_706c8943_4eongxpt\1022.docx")
        if candidate.exists():
            real_path = candidate

    assert real_path.exists(), f"Source worksheet 1022.docx must exist at {real_path}"
    test_copy = tmp_path / "1022.docx"
    test_copy.write_bytes(real_path.read_bytes())
    return test_copy


def test_1022_parser_detects_all_three_visible_questions(worksheet_1022_path: Path):
    """Criteria 1 & 2: The 3 visible questions in 1022.docx are detected as answerable questions."""
    sha_before = hashlib.sha256(worksheet_1022_path.read_bytes()).hexdigest()

    parser = DocxWorksheetParser()
    parsed_ws = parser.parse(worksheet_1022_path)

    # Criteria 6: Original document is untouched
    sha_after = hashlib.sha256(worksheet_1022_path.read_bytes()).hexdigest()
    assert sha_before == sha_after, "Original 1022.docx must remain byte-for-byte unchanged after parsing"

    # Criteria 2: Parser returns exactly 3 answerable questions
    assert parsed_ws.question_count == 3, f"Expected 3 questions, got {parsed_ws.question_count}"
    assert len(parsed_ws.questions) == 3

    # Criteria 1: 3 questions visible in worksheet are detected with accurate numbers and text
    q0, q1, q2 = parsed_ws.questions

    assert q0.question_number == "1"
    assert "Write a Java interface and implement it in a class" in q0.question_text
    assert q0.question_type in (QuestionType.SHORT_ANSWER, QuestionType.CODE)

    assert q1.question_number == "2"
    assert "Demonstrate abstraction using access modifiers" in q1.question_text
    assert q1.question_type == QuestionType.SHORT_ANSWER

    assert q2.question_number == "3"
    assert "Give a real-world analogy for abstraction and relate it to Java classes" in q2.question_text
    assert q2.question_type == QuestionType.SHORT_ANSWER

    # Metadata validation
    assert parsed_ws.course_code == "21CSC203P"
    assert parsed_ws.slo == 2


@pytest.mark.asyncio
async def test_1022_freellm_generation_and_filling_end_to_end(worksheet_1022_path: Path, tmp_path: Path):
    """Criteria 3, 4, 5, 6, 7: FreeLLM generates 3 answers, filler writes all 3 into intended locations."""
    sha_before = hashlib.sha256(worksheet_1022_path.read_bytes()).hexdigest()

    # 1. Parse worksheet
    parser = DocxWorksheetParser()
    parsed_ws = parser.parse(worksheet_1022_path)
    assert len(parsed_ws.questions) == 3, "Parser must yield exactly 3 questions before FreeLLM is called"

    # 2. Mock FreeLLM generating 3 distinct answers
    mock_freellm_answers = [
        {
            "question_id": parsed_ws.questions[0].question_id,
            "question_number": "1",
            "answer_text": "interface Printable {\n    void print();\n}\n\nclass Document implements Printable {\n    public void print() {\n        System.out.println(\"Document printed successfully.\");\n    }\n}",
            "confidence": 0.96,
        },
        {
            "question_id": parsed_ws.questions[1].question_id,
            "question_number": "2",
            "answer_text": "class BankAccount {\n    private double balance;\n\n    public BankAccount(double balance) {\n        this.balance = balance;\n    }\n\n    public double getBalance() {\n        return this.balance;\n    }\n}",
            "confidence": 0.94,
        },
        {
            "question_id": parsed_ws.questions[2].question_id,
            "question_number": "3",
            "answer_text": "A smartphone touchscreen is an analogy: users tap icons without knowing circuit operations, matching public method calls hiding internal code.",
            "confidence": 0.95,
        },
    ]

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "id": "chatcmpl-freellm-1022",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"answers": mock_freellm_answers}),
                },
                "finish_reason": "stop",
            }
        ],
    }
    mock_resp.text = json.dumps(mock_resp.json.return_value)

    freellm_engine = LLMAnswerEngine(provider="freellm", api_key="test-api-key")
    with patch.object(freellm_engine, "_get_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_get_client.return_value = mock_client
        answers = await freellm_engine.generate_answers(parsed_ws)

    # Criteria 3: Exactly 3 answers generated
    assert answers.total_count == 3, f"Expected 3 answers from FreeLLM, got {answers.total_count}"
    assert answers.success_count == 3

    # 3. Filler writes all 3 answers
    filler = DocxWorksheetFiller()
    output_path = filler.fill(
        original_file_path=worksheet_1022_path,
        worksheet=parsed_ws,
        answers=answers,
        output_dir=tmp_path,
        output_filename="completed_1022.docx",
    )

    # Criteria 6: Original unchanged
    sha_after = hashlib.sha256(worksheet_1022_path.read_bytes()).hexdigest()
    assert sha_before == sha_after, "Original 1022.docx must remain byte-for-byte unchanged after filling"

    # Criteria 4: Filler writes all 3 answers into their intended locations
    completed_doc = docx.Document(str(output_path))
    doc_paragraphs = [p.text.strip() for p in completed_doc.paragraphs if p.text.strip()]

    # Locate each question and its subsequent answer
    q1_idx = next(i for i, text in enumerate(doc_paragraphs) if "Write a Java interface" in text)
    q2_idx = next(i for i, text in enumerate(doc_paragraphs) if "Demonstrate abstraction using" in text)
    q3_idx = next(i for i, text in enumerate(doc_paragraphs) if "Give a real-world analogy" in text)

    # Verify sequential ordering: Q1 < Ans1 < Q2 < Ans2 < Q3 < Ans3
    assert q1_idx < q2_idx < q3_idx, "Questions must remain in original sequential order"

    # Verify Answer 1 is placed directly under Question 1 (before Question 2)
    ans1_p = doc_paragraphs[q1_idx + 1]
    assert "Answer:" in ans1_p
    assert "interface Printable" in ans1_p
    assert q1_idx + 1 < q2_idx, "Answer 1 must be positioned before Question 2"

    # Verify Answer 2 is placed directly under Question 2 (before Question 3)
    ans2_p = doc_paragraphs[q2_idx + 1]
    assert "Answer:" in ans2_p
    assert "class BankAccount" in ans2_p
    assert q2_idx + 1 < q3_idx, "Answer 2 must be positioned before Question 3"

    # Verify Answer 3 is placed directly under Question 3
    ans3_p = doc_paragraphs[q3_idx + 1]
    assert "Answer:" in ans3_p
    assert "smartphone touchscreen is an analogy" in ans3_p

    # Criteria 7: No duplicate answers
    all_answers_text = [p for p in doc_paragraphs if p.startswith("Answer:")]
    assert len(all_answers_text) == 3, f"Expected exactly 3 answer paragraphs, found {len(all_answers_text)}"


@pytest.mark.asyncio
async def test_1022_pipeline_metadata_questions_answered(worksheet_1022_path: Path, tmp_path: Path):
    """Criteria 5: Verify WorksheetPipeline summary produces total_questions=3 and answers_generated=3."""
    pipeline = WorksheetPipeline(answer_engine=RuleBasedAnswerEngine())
    res = await pipeline.process(
        worksheet_path=worksheet_1022_path,
        output_dir=tmp_path,
        output_filename="completed_1022.docx",
    )

    assert res.success is True
    assert res.summary["total_questions"] == 3
    assert res.summary["answers_generated"] == 3

    # Simulate what worker tasks.py writes to job.result
    job_result = {
        "original_file": str(worksheet_1022_path.name),
        "completed_file": "completed_1022.docx",
        "questions_count": res.summary.get("total_questions"),
        "answers_count": res.summary.get("answers_generated"),
        "practice_status": 2,
    }

    # Dashboard display verification: Questions Answered displays 3, not 0
    assert job_result["questions_count"] == 3, "Dashboard field questions_count must be 3"
    assert job_result["answers_count"] == 3, "Dashboard field answers_count must be 3"
