"""Document filling engine for Microsoft Word (.docx) worksheets.

Injects generated answers into a distinct completed copy of the document while:
1. Guaranteeing the original document remains completely untouched on disk.
2. Preserving original font styles, headers, tables, and visual structure.
3. Clearly styling generated answers so they are distinguishable from questions.
4. Using an element-level generic answer-target system to write directly into intended fields.
"""

import hashlib
import logging
from pathlib import Path
from typing import Optional

import docx
from docx.shared import RGBColor

from packages.worksheets.answer_models import GeneratedAnswer, WorksheetAnswers
from packages.worksheets.answer_target import (
    ANSWER_COLOR_RGB,
    MUTED_GRAY_RGB,
    AnswerTargetResolver,
    TargetWriter,
)
from packages.worksheets.header_filler import StudentHeaderFiller
from packages.worksheets.models import ParsedWorksheet
from packages.worksheets.verification import PhysicalDocumentVerifier, VerificationReport

logger = logging.getLogger(__name__)


class DocxWorksheetFiller:
    """Fills a DOCX worksheet with answers, saving to a new identifiable completed file."""

    def __init__(self, student_info: Optional[dict] = None):
        self.resolver = AnswerTargetResolver()
        self.writer = TargetWriter()
        self.header_filler = StudentHeaderFiller(student_info)
        self.last_verification: Optional[VerificationReport] = None

    def fill(
        self,
        original_file_path: Path,
        worksheet: ParsedWorksheet,
        answers: WorksheetAnswers,
        output_dir: Optional[Path] = None,
        output_filename: Optional[str] = None,
    ) -> Path:
        """Fill worksheet with generated solutions and save to a separate completed file.
        
        Args:
            original_file_path: Path to the untouched original worksheet document.
            worksheet: Parsed structural domain model.
            answers: Collection of generated answers.
            output_dir: Destination directory for the completed file.
            output_filename: Optional custom filename for the completed document.
            
        Returns:
            Path to the saved completed document.
            
        Raises:
            FileNotFoundError: If original document is missing.
            ValueError: If target path collides with the original file.
            WorksheetFillingError: If an answer target cannot be resolved.
            RuntimeError: If original file integrity is violated.
        """
        orig_path = Path(original_file_path)
        if not orig_path.exists():
            raise FileNotFoundError(f"Original worksheet not found: {orig_path}")

        # Record hash before opening to guarantee original document immutability
        hash_before = hashlib.sha256(orig_path.read_bytes()).hexdigest()

        # Destination resolution
        out_dir = Path(output_dir) if output_dir else orig_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        is_pdf = orig_path.suffix.lower() == ".pdf"
        target_name = output_filename or (f"completed_{orig_path.stem}.docx" if is_pdf else f"completed_{orig_path.name}")
        if is_pdf and not target_name.lower().endswith(".docx"):
            target_name = f"{Path(target_name).stem}.docx"
        completed_path = out_dir / target_name

        if completed_path.resolve() == orig_path.resolve():
            raise ValueError(
                "Completed file path cannot be identical to original file! "
                "Original document must remain untouched."
            )

        if is_pdf:
            # Create a structured companion DOCX document for PDF worksheets
            doc = docx.Document()
            meta_table = doc.add_table(rows=2, cols=2)
            meta_table.style = 'Table Grid'
            student_info = getattr(self.header_filler, "student_info", {})
            r0 = meta_table.rows[0].cells
            r0[0].text = f"Student Name: {student_info.get('name', 'G R HEMANTH')}"
            r0[1].text = f"Reg. No.: {student_info.get('reg_no', 'RA2511003011819')}"
            r1 = meta_table.rows[1].cells
            r1[0].text = f"Branch: {student_info.get('branch', 'COMPUTER SCIENCE AND ENGINEERING')}"
            r1[1].text = f"Date: {student_info.get('date', '01-01-2026')}"

            if worksheet.title:
                doc.add_heading(worksheet.title, level=1)

            for question in worksheet.questions:
                answer = answers.get_answer(question.question_id)
                if not answer and question.question_number:
                    answer = answers.get_answer_by_number(question.question_number)
                if not answer:
                    answer = GeneratedAnswer(
                        question_id=question.question_id,
                        question_number=question.question_number,
                        question_type=question.question_type,
                        answer_text="[Answer pending manual review]",
                        confidence=0.0,
                    )
                qp = doc.add_paragraph()
                q_label = f"Question {question.question_number}: " if question.question_number else ""
                q_run = qp.add_run(f"{q_label}{question.question_text}")
                q_run.bold = True

                ap = doc.add_paragraph()
                ans_run = ap.add_run(answer.answer_text)
                ans_run.font.color.rgb = ANSWER_COLOR_RGB
        else:
            # Open in memory
            doc = docx.Document(str(orig_path))

            # 0. Fill Student Identification Metadata (Name, Reg. No., Branch, Date)
            self.header_filler.fill_header(doc)

            # Process each question using generic answer-target resolution
            for question in worksheet.questions:
                answer = answers.get_answer(question.question_id)
                if not answer:
                    # Try lookup by question number
                    if question.question_number:
                        answer = answers.get_answer_by_number(question.question_number)

                if not answer:
                    answer = GeneratedAnswer(
                        question_id=question.question_id,
                        question_number=question.question_number,
                        question_type=question.question_type,
                        answer_text="[Answer pending manual review]",
                        confidence=0.0,
                    )

                # 1. Require exactly one resolved writable target
                target = self.resolver.resolve(doc, question, worksheet.questions)

                # 2. Write the formatted answer into the resolved target location
                self.writer.write(doc, target, question, answer)

        # Save to completed path ONLY
        doc.save(str(completed_path))

        # Safety check: Verify original file was not altered in any way
        hash_after = hashlib.sha256(orig_path.read_bytes()).hexdigest()
        if hash_before != hash_after:
            raise RuntimeError(
                f"FATAL: Original document at {orig_path} was modified during filling! "
                "Original documents must remain immutable."
            )

        # 3. Comprehensive Physical XML & Document Structure Verification
        self.last_verification = PhysicalDocumentVerifier.verify(
            original_path=orig_path,
            completed_path=completed_path,
            original_hash_before=hash_before,
            worksheet=worksheet,
            answers=answers,
        )

        logger.info(
            "Successfully completed worksheet saved to %s (Original intact: %s, Valid: %s)",
            completed_path,
            orig_path.name,
            self.last_verification.is_valid,
        )
        return completed_path
