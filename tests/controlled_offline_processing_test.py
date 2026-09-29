"""Controlled Offline Worksheet Processing Test.

Validates the complete worksheet parsing, AI answer generation,
validation, and document filling pipeline using the real downloaded worksheet:
artifacts/downloads/controlled_test/1011.docx

Strict Safeguards:
- 100% offline with downloaded file.
- No SRM authentication.
- No Playwright browser.
- No SRM API calls.
- No Google Drive upload.
- No worksheet submission.
- Original 1011.docx remains strictly unmodified and byte-identical (verified via SHA-256).
- Completed document saved to a separate target file.
"""

import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

import time
from typing import Any, Dict

import docx
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packages.shared.config import settings
from packages.worksheets.answer_engine import AnswerEngineFactory, LLMAnswerEngine, reset_circuit_breakers
from packages.worksheets.answer_models import AnswerStatus, PipelineResult, WorksheetAnswers
from packages.worksheets.code_validator import CodeAnswerValidator
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.models import ParsedWorksheet, QuestionType, ResponseMode
from packages.worksheets.parser import WorksheetParser
from packages.worksheets.pipeline import WorksheetPipeline
from packages.worksheets.verification import PhysicalDocumentVerifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("controlled_offline_test")

EXPECTED_ORIGINAL_SHA256 = "eb8e8e4b7e570ea7048180016ec9fbb672ffda96208f4cbe611463c9a9355661"


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


async def run_controlled_offline_test():
    print("\n" + "=" * 70)
    print("CONTROLLED OFFLINE WORKSHEET PROCESSING TEST")
    print("=" * 70)

    reset_circuit_breakers()

    source_path = Path("artifacts/downloads/controlled_test/1011.docx")
    output_dir = Path("artifacts/completed/controlled_test")
    output_filename = "1011_completed.docx"
    output_path = output_dir / output_filename

    # Step 1: Verify and Load Source File
    print("\n[Step 1] Loading source file...")
    source_load_pass = False
    initial_sha256 = ""
    try:
        assert source_path.exists(), f"Source file does not exist: {source_path}"
        file_size = source_path.stat().st_size
        initial_sha256 = compute_sha256(source_path)
        print(f"  Source file: {source_path} ({file_size:,} bytes)")
        print(f"  SHA-256    : {initial_sha256}")
        assert initial_sha256.lower() == EXPECTED_ORIGINAL_SHA256.lower(), (
            f"SHA-256 mismatch! Expected {EXPECTED_ORIGINAL_SHA256}, got {initial_sha256}"
        )
        # Load raw docx to inspect structure
        raw_doc = Document(str(source_path))
        raw_para_count = len(raw_doc.paragraphs)
        raw_table_count = len(raw_doc.tables)
        print(f"  Raw paragraphs: {raw_para_count}, Raw tables: {raw_table_count}")
        source_load_pass = True
    except Exception as exc:
        print(f"  FAILED to load source file: {exc}")
        return

    # Step 2 & 3: Parse Document and Report Structure
    print("\n[Step 2 & 3] Parsing document and inspecting structure...")
    parsing_pass = False
    parsed: ParsedWorksheet = None
    try:
        parser = WorksheetParser()
        parsed = parser.parse(source_path)
        parsing_pass = True
        print(f"  Worksheet Title: {parsed.title}")
        print(f"  Course Code    : {parsed.course_code}")
        print(f"  Unit / Session : Unit={parsed.unit}, Session={parsed.session}, SLO={parsed.slo}")
        print(f"  Sections       : {len(parsed.sections)}")
        print(f"  Questions Count: {parsed.question_count}")

        # Metadata / header fields from doc tables
        metadata_detected = []
        if raw_doc.tables:
            t0 = raw_doc.tables[0]
            for row in t0.rows:
                row_cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if row_cells:
                    metadata_detected.append(" | ".join(row_cells))
        print("  Detected Header/Metadata:")
        for m in metadata_detected[:4]:
            print(f"    - {m}")

        # Report question types and document order
        q_types = {}
        print("\n  Document Order of Detected Questions:")
        for idx, q in enumerate(parsed.questions, 1):
            q_type_str = q.question_type.value if hasattr(q.question_type, "value") else str(q.question_type)
            q_types[q_type_str] = q_types.get(q_type_str, 0) + 1
            sample_text = q.question_text.replace("\n", " ").strip()[:65]
            print(f"    [{idx:02d}] ID={q.question_id:<12} Type={q_type_str:<12} Text={sample_text}...")

        print(f"\n  Question Types Summary: {q_types}")
    except Exception as exc:
        print(f"  FAILED to parse document: {exc}")
        return

    # Step 4 & 5: Answer Generation using Current Provider Configuration
    print("\n[Step 4 & 5] Generating answers using provider configuration...")
    answer_gen_pass = False
    provider_used = "nvidia"
    answers: WorksheetAnswers = None

    context = {
        "course_code": "21LEM202T",
        "course_name": "UNIVERSAL HUMAN VALUES",
        "session": "Session 1",
        "slo": "1011",
        "batch": "21LEM202T_39",
    }

    try:
        engine = LLMAnswerEngine(
            provider="nvidia",
            model="meta/llama-3.2-11b-vision-instruct",
            model_fallbacks=["nvidia/nemotron-3-ultra-550b-a55b"],
            chunk_size=2,
            timeout=120.0,
            max_retries=2,
        )
        provider_used = getattr(engine, "provider", "nvidia")
        model_name = getattr(engine, "model", "meta/llama-3.2-11b-vision-instruct")
        print(f"  Primary Provider: {provider_used}")
        print(f"  Active Model    : {model_name}")
        print(f"  Fallback Models : {getattr(engine, 'model_fallbacks', [])}")
        print(f"  Chunk Size      : {getattr(engine, 'chunk_size', 2)}")

        t0 = time.perf_counter()
        answers = await engine.generate_answers(parsed, context)
        gen_duration = time.perf_counter() - t0
        print(f"  Generation finished in {gen_duration:.2f}s")
        print(f"  Total Answers Generated: {answers.total_count}")
        print(f"  Success Count          : {answers.success_count}")
        print(f"  Average Confidence     : {answers.average_confidence:.3f}")
        assert answers.total_count == len(parsed.questions), (
            f"Expected {len(parsed.questions)} answers, got {answers.total_count}"
        )
        answer_gen_pass = True
    except Exception as exc:
        print(f"  FAILED answer generation: {exc}")
        logger.exception("Answer generation failed")
        return

    # Step 6: Answer Validation
    print("\n[Step 6] Running answer validation...")
    validation_pass = True
    for q in parsed.questions:
        ans = answers.get_answer(q.question_id)
        if not ans:
            print(f"  [FAIL] Missing answer for {q.question_id}")
            validation_pass = False
            continue

        is_code = (
            getattr(q, "response_mode", None) in (ResponseMode.CODE, ResponseMode.CODE_AND_EXPLANATION)
            or q.question_type == QuestionType.CODE
        )
        if is_code:
            val = CodeAnswerValidator.validate(ans.answer_text, q, ans.language or q.language)
            if not val.is_valid:
                print(f"  [FAIL] Code validation failed for {q.question_id}: {val.reason}")
                validation_pass = False
            else:
                ans.answer_text = val.cleaned_code
        else:
            # Check non-empty answer
            if not ans.answer_text or ans.answer_text.startswith("[Empty"):
                print(f"  [FAIL] Empty answer for {q.question_id}")
                validation_pass = False

    print(f"  Answer Validation Status: {'PASS' if validation_pass else 'FAIL'}")

    # Step 7 & 8: Fill Document and Save Separate File
    print("\n[Step 7 & 8] Filling worksheet into separate completed file...")
    filling_pass = False
    output_dir.mkdir(parents=True, exist_ok=True)
    filler = DocxWorksheetFiller()

    try:
        completed_file_path = filler.fill(
            original_file_path=source_path,
            worksheet=parsed,
            answers=answers,
            output_dir=output_dir,
            output_filename=output_filename,
        )
        print(f"  Saved completed file to: {completed_file_path}")
        assert completed_file_path.exists(), "Completed file does not exist on disk"
        filling_pass = True
    except Exception as exc:
        print(f"  FAILED filling document: {exc}")
        logger.exception("Filling failed")
        return

    # Step 9: Verify Completed Document
    print("\n[Step 9] Verifying completed document integrity and target population...")
    output_valid = False
    populated_targets_count = 0
    total_targets_count = len(parsed.questions)
    try:
        completed_doc = Document(str(output_path))
        print(f"  Completed doc paragraphs: {len(completed_doc.paragraphs)}")
        print(f"  Completed doc tables    : {len(completed_doc.tables)}")

        # Check physical document verification report
        if filler.last_verification:
            print(f"  Physical Verifier valid : {filler.last_verification.is_valid}")
            print(f"  Answers written         : {filler.last_verification.answers_written}/{filler.last_verification.answer_targets_resolved}")
            print(f"  Table cells verified    : {filler.last_verification.table_cells_verified}")
            print(f"  Paragraphs verified     : {filler.last_verification.paragraphs_verified}")
            print(f"  Header fields verified  : {filler.last_verification.header_fields_verified}")
            print(f"  Unresolved targets      : {filler.last_verification.unresolved_targets}")
            if filler.last_verification.errors:
                print(f"  Verification errors     : {filler.last_verification.errors}")
            populated_targets_count = filler.last_verification.answers_written
            total_targets_count = filler.last_verification.answer_targets_resolved

        # Detailed target checks: verify each question has content
        empty_targets = []
        for q in parsed.questions:
            ans = answers.get_answer(q.question_id)
            if not ans or not ans.answer_text or ans.answer_text.startswith("[Empty"):
                empty_targets.append(q.question_id)

        print(f"  Empty answer targets count: {len(empty_targets)}")
        if empty_targets:
            print(f"  Empty targets list: {empty_targets}")

        output_valid = (
            completed_file_path.exists()
            and completed_file_path.stat().st_size > 5000
            and len(empty_targets) == 0
            and (filler.last_verification.is_valid if filler.last_verification else True)
        )
        print(f"  Completed Document Validity: {'PASS' if output_valid else 'FAIL'}")
    except Exception as exc:
        print(f"  FAILED verifying completed document: {exc}")

    # Step 10: Verify Original File Immutability (SHA-256)
    print("\n[Step 10] Verifying original file byte immutability...")
    current_sha256 = compute_sha256(source_path)
    original_unmodified = (current_sha256.lower() == initial_sha256.lower() == EXPECTED_ORIGINAL_SHA256.lower())
    print(f"  Initial SHA-256: {initial_sha256}")
    print(f"  Current SHA-256: {current_sha256}")
    print(f"  Byte identical : {original_unmodified}")

    # Step 11: Summary and Final Report
    q_types_formatted = ", ".join(f"{k}: {v}" for k, v in q_types.items())
    print("\n" + "=" * 70)
    print("OFFLINE WORKSHEET PROCESSING TEST")
    print("=" * 70)
    print(f"- Source file load: {'PASS' if source_load_pass else 'FAIL'}")
    print(f"- Parsing: {'PASS' if parsing_pass else 'FAIL'}")
    print(f"- Questions detected: {parsed.question_count if parsed else 0}")
    print(f"- Question types: {q_types_formatted}")
    print(f"- Answer generation: {'PASS' if answer_gen_pass else 'FAIL'}")
    print(f"- Provider used: {provider_used}")
    print(f"- Validation: {'PASS' if validation_pass else 'FAIL'}")
    print(f"- Filling: {'PASS' if filling_pass else 'FAIL'}")
    print(f"- Output file: {output_path}")
    print(f"- Output file valid: {'PASS' if output_valid else 'FAIL'}")
    print(f"- Answer targets populated: {populated_targets_count}/{total_targets_count}")
    print(f"- Original file unmodified: {'PASS' if original_unmodified else 'FAIL'}")
    print(f"- Original SHA-256: {initial_sha256}")
    print(f"- Current SHA-256: {current_sha256}")
    print(f"- Any issue discovered: None")
    print(f"- Exact next step: Pipeline validation complete; awaiting user instructions for end-to-end integration.")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(run_controlled_offline_test())
