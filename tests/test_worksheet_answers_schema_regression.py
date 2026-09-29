"""Regression tests for WorksheetAnswers schema and list-vs-dict handling."""

import pytest
from packages.worksheets.answer_models import AnswerStatus, GeneratedAnswer, WorksheetAnswers
from packages.worksheets.models import QuestionType, ResponseMode


def test_worksheet_answers_answers_is_list():
    """Verify that WorksheetAnswers.answers is explicitly a List, not a Dict."""
    ans1 = GeneratedAnswer(
        question_id="q1",
        question_number="1",
        question_type=QuestionType.SHORT_ANSWER,
        response_mode=ResponseMode.TEXT,
        answer_text="Sample Answer 1",
        status=AnswerStatus.SUCCESS,
        metadata={"provider": "nvidia"},
    )
    ans2 = GeneratedAnswer(
        question_id="q2",
        question_number="2",
        question_type=QuestionType.LONG_ANSWER,
        response_mode=ResponseMode.TEXT,
        answer_text="Sample Answer 2",
        status=AnswerStatus.SUCCESS,
        metadata={"provider": "nvidia"},
    )

    ws_answers = WorksheetAnswers(
        worksheet_filename="1011.docx",
        answers=[ans1, ans2],
        provider="nvidia",
    )

    # 1. Assert type is list
    assert isinstance(ws_answers.answers, list)
    assert not hasattr(ws_answers.answers, "values")
    assert len(ws_answers.answers) == 2

    # 2. Assert properties
    assert ws_answers.total_count == 2
    assert ws_answers.success_count == 2
    assert ws_answers.get_answer("q1") == ans1
    assert ws_answers.get_answer_by_number("2") == ans2


def test_safe_answer_extraction_handles_list_and_dict():
    """Verify that safe extraction logic handles list without AttributeError."""
    ans1 = GeneratedAnswer(
        question_id="q1",
        question_number="1",
        answer_text="Text 1",
        status=AnswerStatus.SUCCESS,
        metadata={"provider": "nvidia"},
    )
    ans2 = GeneratedAnswer(
        question_id="q2",
        question_number="2",
        answer_text="Text 2",
        status=AnswerStatus.ERROR,
        metadata={"provider": "nvidia"},
    )

    # Production scenario: answers is a list
    ws_answers_list = WorksheetAnswers(
        worksheet_filename="1011.docx",
        answers=[ans1, ans2],
    )

    raw_answers = (
        list(ws_answers_list.answers.values())
        if isinstance(ws_answers_list.answers, dict)
        else ws_answers_list.answers
    )
    assert isinstance(raw_answers, list)
    assert len(raw_answers) == 2
    successful = [a for a in raw_answers if getattr(a, "status", None) == AnswerStatus.SUCCESS]
    assert len(successful) == 1
    assert successful[0].question_id == "q1"

    # Hypothetical scenario: dict wrapping
    mock_dict = {"q1": ans1, "q2": ans2}
    raw_from_dict = (
        list(mock_dict.values())
        if isinstance(mock_dict, dict)
        else mock_dict
    )
    assert isinstance(raw_from_dict, list)
    assert len(raw_from_dict) == 2
    successful_dict = [a for a in raw_from_dict if getattr(a, "status", None) == AnswerStatus.SUCCESS]
    assert len(successful_dict) == 1


def test_verification_report_schema_fields():
    """Verify that VerificationReport has real production fields and no confidence_score."""
    from packages.worksheets.verification import VerificationReport

    report = VerificationReport(
        original_file="1011.docx",
        completed_file="completed_1011.docx",
        original_sha256_matches=True,
        questions_detected=14,
        answer_targets_resolved=48,
        answers_generated=14,
        answers_written=48,
        unresolved_targets=0,
        duplicate_answers=0,
        header_fields_verified=["name", "reg_no", "branch", "date"],
        table_cells_verified=37,
        paragraphs_verified=11,
        is_valid=True,
        errors=[],
    )

    # 1. Assert confidence_score does NOT exist
    assert not hasattr(report, "confidence_score")

    # 2. Assert real fields exist and contain expected values
    assert report.is_valid is True
    assert report.original_sha256_matches is True
    assert report.answers_written == 48
    assert report.answer_targets_resolved == 48
    assert report.table_cells_verified == 37
    assert report.paragraphs_verified == 11
    assert len(report.header_fields_verified) == 4
    assert len(report.errors) == 0

    # 3. Assert string summary does not raise
    summary = report.to_summary_str()
    assert "Original unchanged: YES" in summary

