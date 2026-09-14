"""Document filling engine for Microsoft Word (.docx) worksheets.

Injects generated answers into a distinct completed copy of the document while:
1. Guaranteeing the original document remains completely untouched on disk.
2. Preserving original font styles, headers, tables, and visual structure.
3. Clearly styling generated answers so they are distinguishable from questions.
"""

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import docx
from docx.shared import Inches, Pt, RGBColor
from docx.text.paragraph import Paragraph

from packages.worksheets.answer_models import GeneratedAnswer, WorksheetAnswers
from packages.worksheets.models import ParsedQuestion, ParsedWorksheet, QuestionType

logger = logging.getLogger(__name__)

# Distinct styling colors for answers
ANSWER_COLOR_RGB = RGBColor(0x1F, 0x4E, 0x79)  # Professional navy blue
MUTED_GRAY_RGB = RGBColor(0x59, 0x59, 0x59)


class DocxWorksheetFiller:
    """Fills a DOCX worksheet with answers, saving to a new identifiable completed file."""

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
        """
        orig_path = Path(original_file_path)
        if not orig_path.exists():
            raise FileNotFoundError(f"Original worksheet not found: {orig_path}")

        # Record hash before opening
        hash_before = hashlib.sha256(orig_path.read_bytes()).hexdigest()

        # Destination resolution
        out_dir = Path(output_dir) if output_dir else orig_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        target_name = output_filename or f"completed_{orig_path.name}"
        completed_path = out_dir / target_name

        if completed_path.resolve() == orig_path.resolve():
            raise ValueError(
                "Completed file path cannot be identical to original file! "
                "Original document must remain untouched."
            )

        # Open in memory
        doc = docx.Document(str(orig_path))

        # Process each question
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

            # Check if this question came from a table
            table_loc = question.source_location.get("table_index")
            row_loc = question.source_location.get("row_index")

            if table_loc is not None and row_loc is not None and table_loc < len(doc.tables):
                self._fill_table_question(doc.tables[table_loc], row_loc, question, answer)
            else:
                self._fill_paragraph_question(doc, question, answer)

        # Save to completed path ONLY
        doc.save(str(completed_path))

        # Safety check: Verify original file was not altered in any way
        hash_after = hashlib.sha256(orig_path.read_bytes()).hexdigest()
        if hash_before != hash_after:
            raise RuntimeError(
                f"FATAL: Original document at {orig_path} was modified during filling! "
                "Original documents must remain immutable."
            )

        logger.info(
            "Successfully completed worksheet saved to %s (Original intact: %s)",
            completed_path,
            orig_path.name,
        )
        return completed_path

    def _fill_paragraph_question(
        self,
        doc: docx.Document,
        question: ParsedQuestion,
        answer: GeneratedAnswer,
    ) -> None:
        """Locate question in document paragraphs and insert formatted solution."""
        q_clean = question.question_text.splitlines()[0].strip()[:40].lower()
        q_num = question.question_number

        target_idx = -1
        for idx, p in enumerate(doc.paragraphs):
            p_text = p.text.strip().lower()
            if not p_text:
                continue

            # Match question number prefix (e.g. "1.", "q1.", "activity 1")
            matches_num = False
            if q_num:
                patterns = [
                    f"{q_num}.", f"{q_num})", f"q{q_num}.", f"q{q_num}:",
                    f"activity {q_num}", f"activity {q_num}:"
                ]
                matches_num = any(p_text.startswith(pat) or f" {pat}" in p_text for pat in patterns)

            if matches_num and (q_clean in p_text or len(q_clean) < 10):
                target_idx = idx
                break
            elif q_clean in p_text and len(q_clean) > 15:
                target_idx = idx
                break

        if target_idx == -1:
            # Fallback: could not locate exact paragraph, append at end of document
            self._append_answer_paragraphs(doc.paragraphs[-1], answer)
            return

        # Advance beyond options / context paragraphs if present
        last_block_p = doc.paragraphs[target_idx]
        current_idx = target_idx + 1

        if question.question_type == QuestionType.MCQ:
            # Advance past options (A., B., C., D.)
            while current_idx < len(doc.paragraphs):
                p_curr = doc.paragraphs[current_idx]
                t = p_curr.text.strip()
                if not t:
                    current_idx += 1
                    continue
                # If starts with an option key (A., B., (a), etc.)
                if re.match(r"^(?:\([A-Ea-e]\)|\[[A-Ea-e]\]|[A-Ea-e][\.\)])\s+", t):
                    last_block_p = p_curr

                    # Highlight/bold the selected option if this matches
                    if answer.selected_option and t.upper().startswith(answer.selected_option.upper()):
                        for run in p_curr.runs:
                            run.bold = True
                            run.font.color.rgb = ANSWER_COLOR_RGB
                    current_idx += 1
                else:
                    break
        elif question.context_or_activity:
            # Advance past activity description and objective paragraphs
            while current_idx < len(doc.paragraphs):
                p_curr = doc.paragraphs[current_idx]
                t = p_curr.text.strip()
                if not t:
                    current_idx += 1
                    continue
                if any(t.startswith(prefix) for prefix in [
                    "🧩 Activity:", "Activity:", "🎯 Learning Objective:",
                    "Learning Objective:", "📌 Solution/Outcome:", "Solution/Outcome:",
                    "💡 Outcome:", "Outcome:", "Deliverable:", "Requirements:"
                ]):
                    last_block_p = p_curr
                    current_idx += 1
                elif not re.match(r"^(?:\d+[\.\)]|q\d+|activity\s*\d+)", t, re.IGNORECASE):
                    # Part of activity description
                    last_block_p = p_curr
                    current_idx += 1
                else:
                    break

        # Insert answer immediately after last_block_p
        self._append_answer_paragraphs(last_block_p, answer)

    def _append_answer_paragraphs(
        self,
        anchor_paragraph: Paragraph,
        answer: GeneratedAnswer,
    ) -> None:
        """Insert formatted answer paragraphs following the anchor paragraph."""
        lines = answer.answer_text.splitlines()
        parent_doc = anchor_paragraph._parent
        current_anchor = anchor_paragraph

        # Insert first answer line with bold "Answer:" label
        new_p_elm = anchor_paragraph._p.getparent()._new_p()
        current_anchor._p.addnext(new_p_elm)
        ans_p = Paragraph(new_p_elm, parent_doc)

        label_run = ans_p.add_run("Answer: ")
        label_run.bold = True
        label_run.font.color.rgb = ANSWER_COLOR_RGB
        label_run.font.size = Pt(10.5)

        if lines:
            text_run = ans_p.add_run(lines[0])
            text_run.font.color.rgb = ANSWER_COLOR_RGB
            text_run.font.size = Pt(10.5)
        current_anchor = ans_p

        # For multi-line responses (short/long answers)
        for line in lines[1:]:
            if not line.strip():
                continue
            line_elm = current_anchor._p.getparent()._new_p()
            current_anchor._p.addnext(line_elm)
            line_p = Paragraph(line_elm, parent_doc)

            run = line_p.add_run(line)
            # If section header in long answer (e.g. "1. Technical Architecture:")
            if re.match(r"^\d+\.\s+[A-Za-z]", line.strip()):
                run.bold = True
            run.font.color.rgb = ANSWER_COLOR_RGB
            run.font.size = Pt(10.5)
            current_anchor = line_p

    def _fill_table_question(
        self,
        table: docx.table.Table,
        row_idx: int,
        question: ParsedQuestion,
        answer: GeneratedAnswer,
    ) -> None:
        """Insert answer into the corresponding table row."""
        if row_idx >= len(table.rows):
            return

        row = table.rows[row_idx]
        header_cells = [c.text.strip().lower() for c in table.rows[0].cells]

        # Check if an Answer / Solution column exists
        ans_col_idx = -1
        for idx, h in enumerate(header_cells):
            if any(term in h for term in ["answer", "solution", "response", "output"]):
                ans_col_idx = idx
                break

        if ans_col_idx != -1 and ans_col_idx < len(row.cells):
            # Target existing answer column
            cell = row.cells[ans_col_idx]
            p = cell.paragraphs[0] if cell.paragraphs else cell.add_paragraph()
            p.text = answer.answer_text
            for run in p.runs:
                run.font.color.rgb = ANSWER_COLOR_RGB
                run.bold = True
        else:
            # Append answer inside the question column
            q_col_idx = 1 if len(row.cells) > 1 else 0
            cell = row.cells[q_col_idx]
            ans_p = cell.add_paragraph()
            lbl = ans_p.add_run("Answer: ")
            lbl.bold = True
            lbl.font.color.rgb = ANSWER_COLOR_RGB
            txt = ans_p.add_run(answer.answer_text)
            txt.font.color.rgb = ANSWER_COLOR_RGB
