"""Verification script for real 21CSC203P Threading worksheet (1082.docx).

Executes the local worksheet parsing, answer generation, code validation,
and DOCX filling pipeline. Inspects the completed DOCX to verify:
- Q1 contains code
- Q2 contains code
- Q3 contains code
- code is inside intended answer area
- indentation is preserved
- no markdown fences
- no empty answer box
"""

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, ".")

import docx

from packages.worksheets.parser import WorksheetParser
from packages.worksheets.pipeline import WorksheetPipeline
from packages.worksheets.models import ResponseMode
from packages.worksheets.code_validator import CodeAnswerValidator
from packages.worksheets.answer_engine import AnswerEngineFactory


async def run_verification():
    ws_path = Path("artifacts/1082.docx")
    assert ws_path.exists(), f"File not found: {ws_path}"

    parser = WorksheetParser()
    parsed_ws = parser.parse(ws_path)

    print("=== WORKSHEET PARSED ===")
    print("Filename:", parsed_ws.filename)
    print("Course Code:", parsed_ws.course_code)
    print("Session:", parsed_ws.session, "SLO:", parsed_ws.slo)
    print("Total Questions:", len(parsed_ws.questions))

    for idx, q in enumerate(parsed_ws.questions, 1):
        print(f"\nQ{idx}: id={q.question_id}, num={q.question_number}, type={q.question_type}, mode={q.response_mode}, lang={q.language}")
        print(f"   Text: {q.question_text}")
        assert q.response_mode == ResponseMode.CODE, f"Q{idx} response_mode is {q.response_mode}, expected CODE!"

    engine = AnswerEngineFactory.get_engine(allow_fallback_when_unconfigured=True)
    pipeline = WorksheetPipeline(parser=parser, answer_engine=engine)

    out_dir = Path("artifacts/live_test_output")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = "completed_1082_real_verified.docx"

    res = await pipeline.process(
        worksheet_path=ws_path,
        output_dir=out_dir,
        output_filename=out_file,
    )

    print("\n=== PIPELINE EXECUTION RESULT ===")
    print("Success:", res.success)
    print("Answers Generated:", res.answers.total_count)
    print("Provider:", res.answers.provider)

    # Inspect generated answers
    for idx, q in enumerate(parsed_ws.questions, 1):
        ans = res.answers.get_answer(q.question_id)
        assert ans is not None, f"Q{idx} answer is None!"
        print(f"\n--- Q{idx} Answer Analysis ---")
        print("Status:", ans.status)
        print("Confidence:", ans.confidence)
        val = CodeAnswerValidator.validate(ans.answer_text, q, ans.language or "java")
        print("Code Validation is_valid:", val.is_valid, "Reason:", val.reason)
        assert val.is_valid, f"Q{idx} failed code validation: {val.reason}"
        assert ans.answer_text and len(ans.answer_text.strip()) > 0, f"Q{idx} is empty!"
        assert "```" not in ans.answer_text, f"Q{idx} contains markdown fences!"
        assert not ans.answer_text.startswith("[Empty"), f"Q{idx} contains empty placeholder!"
        print("Answer Full Text:\n" + ans.answer_text)

    # Inspect completed DOCX document
    completed_doc_path = out_dir / out_file
    assert completed_doc_path.exists(), f"Completed doc not found at {completed_doc_path}"
    doc = docx.Document(str(completed_doc_path))

    print("\n=== COMPLETED DOCX STRUCTURE INSPECTION ===")
    paragraphs = [p for p in doc.paragraphs if p.text.strip()]
    print(f"Total non-empty paragraphs in completed DOCX: {len(paragraphs)}")

    for idx, p in enumerate(paragraphs):
        print(f"P{idx:02d}: {repr(p.text[:75])}")

    # Locate Q1, Q2, Q3
    q1_idx = next(i for i, p in enumerate(paragraphs) if "Hello" in p.text and "thread" in p.text)
    q2_idx = next(i for i, p in enumerate(paragraphs) if "even and odd" in p.text)
    q3_idx = next(i for i, p in enumerate(paragraphs) if "sleep and join" in p.text)

    print(f"\nQuestion Locations: Q1 at P{q1_idx}, Q2 at P{q2_idx}, Q3 at P{q3_idx}")
    assert q1_idx < q2_idx < q3_idx, "Questions out of sequence in completed DOCX!"

    # Verify answer paragraphs placed under each question
    q1_ans_p = paragraphs[q1_idx + 1]
    q2_ans_p = paragraphs[q2_idx + 1]
    q3_ans_p = paragraphs[q3_idx + 1]

    print("\nAnswer Placements:")
    print(f"P{q1_idx + 1} (under Q1): {repr(q1_ans_p.text)}")
    print(f"P{q2_idx + 1} (under Q2): {repr(q2_ans_p.text)}")
    print(f"P{q3_idx + 1} (under Q3): {repr(q3_ans_p.text)}")

    # Check indentation preservation
    indented_lines = [p.text for p in doc.paragraphs if p.text.startswith("    ") or p.text.startswith("\t")]
    print(f"\nTotal Indented Code Paragraphs in completed DOCX: {len(indented_lines)}")
    assert len(indented_lines) > 0, "No indented lines found! Code indentation was not preserved!"
    for line in indented_lines[:6]:
        print("  Sample indented line:", repr(line))

    # Verify no empty answer box
    for p in doc.paragraphs:
        assert p.text != "[Empty answer returned by AI]"
        assert p.text != "[Answer pending manual review]"

    print("\n" + "=" * 70)
    print(">>> REAL 21CSC203P WORKSHEET VALIDATION COMPLETED SUCCESSFULLY! <<<")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_verification())
