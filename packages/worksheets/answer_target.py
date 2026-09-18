"""Generic Answer-Target Resolution and Writing System for DOCX Worksheets.

Provides a robust, element-level target resolution engine that maps each parsed
question to its intended writable location (paragraph placeholder, table cell,
bordered box, text box, content control, or anchor) rather than guessing by order.
"""

import logging
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import docx
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Pt, RGBColor
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from packages.worksheets.answer_models import GeneratedAnswer
from packages.worksheets.exceptions import WorksheetFillingError
from packages.worksheets.models import ParsedQuestion, QuestionType

logger = logging.getLogger(__name__)

# Standard styling colors for filled answers
ANSWER_COLOR_RGB = RGBColor(0x1F, 0x4E, 0x79)  # Professional navy blue
MUTED_GRAY_RGB = RGBColor(0x59, 0x59, 0x59)

# XML Namespaces used in DOCX structures
NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "v": "urn:schemas-microsoft-com:vml",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


class AnswerTargetType(str, Enum):
    """Classification of writable answer target locations in DOCX documents."""
    PARAGRAPH_PLACEHOLDER = "PARAGRAPH_PLACEHOLDER"  # Existing paragraph containing "Answer:", "Ans:", etc.
    PARAGRAPH_BOX = "PARAGRAPH_BOX"                  # Bordered or designated empty paragraph for answer
    TABLE_CELL_COLUMN = "TABLE_CELL_COLUMN"          # Dedicated answer column in same row
    TABLE_CELL_ROW = "TABLE_CELL_ROW"                # Dedicated answer row below question (alternating/merged)
    TABLE_CELL_BLANK = "TABLE_CELL_BLANK"            # Blank or bordered cell in same row
    TABLE_CELL_APPEND = "TABLE_CELL_APPEND"          # Append inside question cell (when no dedicated cell)
    TEXT_BOX = "TEXT_BOX"                            # w:txbxContent element in DrawingML or VML
    CONTENT_CONTROL = "CONTENT_CONTROL"              # w:sdtContent structured document tag
    NEW_PARAGRAPH_ANCHOR = "NEW_PARAGRAPH_ANCHOR"    # Fallback: insert new paragraph after question block


class AnswerTarget:
    """Resolved writable target for a single question."""

    def __init__(
        self,
        question_id: str,
        target_type: AnswerTargetType,
        paragraph_index: Optional[int] = None,
        paragraph_obj: Optional[Paragraph] = None,
        table_index: Optional[int] = None,
        row_index: Optional[int] = None,
        col_index: Optional[int] = None,
        cell_obj: Optional[_Cell] = None,
        txbx_element: Optional[Any] = None,
        sdt_element: Optional[Any] = None,
        placeholder_text: Optional[str] = None,
        is_explicit: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.question_id = question_id
        self.target_type = target_type
        self.paragraph_index = paragraph_index
        self.paragraph_obj = paragraph_obj
        self.table_index = table_index
        self.row_index = row_index
        self.col_index = col_index
        self.cell_obj = cell_obj
        self.txbx_element = txbx_element
        self.sdt_element = sdt_element
        self.placeholder_text = placeholder_text
        self.is_explicit = is_explicit
        self.metadata = metadata or {}

    def __repr__(self) -> str:
        return (
            f"<AnswerTarget q={self.question_id} type={self.target_type.value} "
            f"p_idx={self.paragraph_index} tbl={self.table_index} r={self.row_index} c={self.col_index} "
            f"explicit={self.is_explicit}>"
        )


def is_answer_placeholder_text(text: str) -> bool:
    """Check if text is an explicit answer prompt or placeholder."""
    t = text.strip()
    if not t:
        return False
    # Explicit answer labels
    if re.match(r"^(?:Answer|Ans|Solution|Response|Output)\s*[:\-–—]?\s*$", t, re.IGNORECASE):
        return True
    if re.match(r"^(?:Answer|Ans|Solution|Response)\s*[:\-–—]\s*(?:_{2,}|\.{3,}|\[.*?\])?\s*$", t, re.IGNORECASE):
        return True
    if re.match(r"^(?:Write|Enter|Type)\s+(?:your\s+)?answer\s*[:\-–—]?\s*$", t, re.IGNORECASE):
        return True
    if re.match(r"^(?:Your\s+Answer|Student\s+Answer)\s*[:\-–—]?\s*$", t, re.IGNORECASE):
        return True
    if re.match(r"^_{3,}$", t):
        return True
    if t in ["[Answer]", "[Answer Here]", "[Solution]", "[Write your answer here]"]:
        return True
    return False


def find_text_box_in_element(element: Any) -> Optional[Any]:
    """Locate any w:txbxContent element within a paragraph or cell element."""
    txbx = element.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}txbxContent")
    if txbx:
        return txbx[0]
    # Check alternate namespaces if any
    for child in element.iter():
        if child.tag.endswith("txbxContent"):
            return child
    return None


def find_content_control_in_element(element: Any) -> Optional[Any]:
    """Locate any w:sdtContent element within an element."""
    sdts = element.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}sdtContent")
    if sdts:
        return sdts[0]
    for child in element.iter():
        if child.tag.endswith("sdtContent"):
            return child
    return None


def cell_has_borders(cell: _Cell) -> bool:
    """Check if a table cell has explicit border styling."""
    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = tcPr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tcBorders")
    return tcBorders is not None


class AnswerTargetResolver:
    """Resolves exactly one intended writable AnswerTarget for each question."""

    def resolve(
        self,
        doc: docx.Document,
        question: ParsedQuestion,
        all_questions: List[ParsedQuestion],
    ) -> AnswerTarget:
        """Resolve the exact writable answer target in doc for question.
        
        Args:
            doc: python-docx Document object.
            question: The question needing an answer target.
            all_questions: Full list of worksheet questions to determine boundaries.
            
        Returns:
            Resolved AnswerTarget.
            
        Raises:
            WorksheetFillingError: If no valid target can be resolved.
        """
        table_loc = question.source_location.get("table_index")
        row_loc = question.source_location.get("row_index")

        if table_loc is not None and row_loc is not None and table_loc < len(doc.tables):
            return self._resolve_table_target(doc.tables[table_loc], table_loc, row_loc, question)

        return self._resolve_paragraph_target(doc, question, all_questions)

    def _resolve_paragraph_target(
        self,
        doc: docx.Document,
        question: ParsedQuestion,
        all_questions: List[ParsedQuestion],
    ) -> AnswerTarget:
        """Resolve target for paragraph-based question."""
        q_p_idx = question.source_location.get("paragraph_index")
        placeholder_p_idx = question.source_location.get("answer_placeholder_paragraph_index")

        # 1. Direct match from parser metadata if valid
        if placeholder_p_idx is not None and placeholder_p_idx < len(doc.paragraphs):
            p = doc.paragraphs[placeholder_p_idx]
            return AnswerTarget(
                question_id=question.question_id,
                target_type=AnswerTargetType.PARAGRAPH_PLACEHOLDER,
                paragraph_index=placeholder_p_idx,
                paragraph_obj=p,
                placeholder_text=p.text.strip(),
                is_explicit=True,
            )

        # 2. Find question index if missing from source_location
        if q_p_idx is None or q_p_idx >= len(doc.paragraphs):
            q_p_idx = self._find_question_paragraph_index(doc, question)

        if q_p_idx == -1:
            # If question paragraph couldn't be located at all, raise structured error
            raise WorksheetFillingError(
                f"Cannot locate question element in document for question ID '{question.question_id}' "
                f"(number={question.question_number}). No writable target could be resolved."
            )

        # Determine search window: from q_p_idx to next question's start index
        next_q_idx = len(doc.paragraphs)
        q_order = question.source_order
        for other_q in all_questions:
            if other_q.source_order > q_order:
                other_p = other_q.source_location.get("paragraph_index")
                if other_p is not None and other_p > q_p_idx and other_p < next_q_idx:
                    next_q_idx = other_p
                    break

        # 3. Scan window for explicit placeholder, textbox, content control, or bordered box
        last_content_idx = q_p_idx

        for idx in range(q_p_idx + 1, min(next_q_idx, len(doc.paragraphs))):
            p = doc.paragraphs[idx]
            text = p.text.strip()

            # Check for embedded text box in paragraph
            txbx = find_text_box_in_element(p._p)
            if txbx is not None:
                return AnswerTarget(
                    question_id=question.question_id,
                    target_type=AnswerTargetType.TEXT_BOX,
                    paragraph_index=idx,
                    paragraph_obj=p,
                    txbx_element=txbx,
                    is_explicit=True,
                )

            # Check for content control in paragraph
            sdt = find_content_control_in_element(p._p)
            if sdt is not None:
                return AnswerTarget(
                    question_id=question.question_id,
                    target_type=AnswerTargetType.CONTENT_CONTROL,
                    paragraph_index=idx,
                    paragraph_obj=p,
                    sdt_element=sdt,
                    is_explicit=True,
                )

            # Check for explicit answer placeholder text
            if is_answer_placeholder_text(text):
                return AnswerTarget(
                    question_id=question.question_id,
                    target_type=AnswerTargetType.PARAGRAPH_PLACEHOLDER,
                    paragraph_index=idx,
                    paragraph_obj=p,
                    placeholder_text=text,
                    is_explicit=True,
                )

            # Check if paragraph has borders designating an answer box
            pBdr = p._p.get_or_add_pPr().find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pBdr")
            if pBdr is not None and not text:
                return AnswerTarget(
                    question_id=question.question_id,
                    target_type=AnswerTargetType.PARAGRAPH_BOX,
                    paragraph_index=idx,
                    paragraph_obj=p,
                    is_explicit=True,
                )

            # Keep track of last option or description paragraph
            if text:
                last_content_idx = idx

        # 4. Fallback: No explicit placeholder found; insert directly after last question block
        anchor_p = doc.paragraphs[last_content_idx]
        return AnswerTarget(
            question_id=question.question_id,
            target_type=AnswerTargetType.NEW_PARAGRAPH_ANCHOR,
            paragraph_index=last_content_idx,
            paragraph_obj=anchor_p,
            is_explicit=False,
        )

    def _resolve_table_target(
        self,
        table: Table,
        table_idx: int,
        row_idx: int,
        question: ParsedQuestion,
    ) -> AnswerTarget:
        """Resolve target inside a table."""
        if row_idx >= len(table.rows):
            raise WorksheetFillingError(
                f"Table row {row_idx} exceeds table row count ({len(table.rows)}) for question '{question.question_id}'."
            )

        row = table.rows[row_idx]

        # 1. Check if source_location identified an answer column
        ans_col = question.source_location.get("answer_col_index")
        if ans_col is not None and ans_col < len(row.cells):
            return AnswerTarget(
                question_id=question.question_id,
                target_type=AnswerTargetType.TABLE_CELL_COLUMN,
                table_index=table_idx,
                row_index=row_idx,
                col_index=ans_col,
                cell_obj=row.cells[ans_col],
                is_explicit=True,
            )

        # 2. Inspect table header for dedicated Answer / Solution column
        header_cells = [c.text.strip().lower() for c in table.rows[0].cells]
        for idx, h in enumerate(header_cells):
            if any(term in h for term in ["answer", "solution", "response", "output", "result"]):
                if idx < len(row.cells):
                    return AnswerTarget(
                        question_id=question.question_id,
                        target_type=AnswerTargetType.TABLE_CELL_COLUMN,
                        table_index=table_idx,
                        row_index=row_idx,
                        col_index=idx,
                        cell_obj=row.cells[idx],
                        is_explicit=True,
                    )

        # 3. Check for dedicated answer row directly below (row-alternating table pattern)
        if row_idx + 1 < len(table.rows):
            next_row = table.rows[row_idx + 1]
            # If next row has a cell starting with "Answer:" or is empty with borders
            next_c0 = next_row.cells[0]
            next_text = next_c0.text.strip()
            if is_answer_placeholder_text(next_text) or (not next_text and cell_has_borders(next_c0)):
                return AnswerTarget(
                    question_id=question.question_id,
                    target_type=AnswerTargetType.TABLE_CELL_ROW,
                    table_index=table_idx,
                    row_index=row_idx + 1,
                    col_index=0,
                    cell_obj=next_c0,
                    placeholder_text=next_text,
                    is_explicit=True,
                )

        # 4. Check for blank or bordered cell in the same row (other than question cell)
        q_col = question.source_location.get("col_index", 0)
        for c_idx, cell in enumerate(row.cells):
            if c_idx != q_col:
                c_text = cell.text.strip()
                if not c_text or is_answer_placeholder_text(c_text):
                    return AnswerTarget(
                        question_id=question.question_id,
                        target_type=AnswerTargetType.TABLE_CELL_BLANK,
                        table_index=table_idx,
                        row_index=row_idx,
                        col_index=c_idx,
                        cell_obj=cell,
                        placeholder_text=c_text,
                        is_explicit=True,
                    )

        # 5. Check if question cell itself contains a text box
        target_cell = row.cells[min(q_col, len(row.cells) - 1)]
        txbx = find_text_box_in_element(target_cell._tc)
        if txbx is not None:
            return AnswerTarget(
                question_id=question.question_id,
                target_type=AnswerTargetType.TEXT_BOX,
                table_index=table_idx,
                row_index=row_idx,
                col_index=q_col,
                cell_obj=target_cell,
                txbx_element=txbx,
                is_explicit=True,
            )

        # 6. Fallback: Append inside question cell
        return AnswerTarget(
            question_id=question.question_id,
            target_type=AnswerTargetType.TABLE_CELL_APPEND,
            table_index=table_idx,
            row_index=row_idx,
            col_index=q_col,
            cell_obj=target_cell,
            is_explicit=False,
        )

    def _find_question_paragraph_index(self, doc: docx.Document, question: ParsedQuestion) -> int:
        """Find paragraph index matching question text and number."""
        q_clean = question.question_text.splitlines()[0].strip()[:40].lower()
        q_num = question.question_number

        for idx, p in enumerate(doc.paragraphs):
            p_text = p.text.strip().lower()
            if not p_text:
                continue

            matches_num = False
            if q_num:
                patterns = [
                    f"{q_num}.", f"{q_num})", f"q{q_num}.", f"q{q_num}:",
                    f"activity {q_num}", f"activity {q_num}:"
                ]
                matches_num = any(p_text.startswith(pat) or f" {pat}" in p_text for pat in patterns)

            if matches_num and (q_clean in p_text or len(q_clean) < 10):
                return idx
            if q_clean in p_text and len(q_clean) > 15:
                return idx

        return -1


class TargetWriter:
    """Writes answers cleanly into resolved AnswerTargets, preserving styles and structure."""

    def write(
        self,
        doc: docx.Document,
        target: AnswerTarget,
        question: ParsedQuestion,
        answer: GeneratedAnswer,
    ) -> None:
        """Write formatted answer into target location.
        
        Args:
            doc: Document being edited.
            target: Resolved AnswerTarget.
            question: The ParsedQuestion being answered.
            answer: Generated solution.
        """
        # Format answer text based on question type
        formatted_text = self._format_answer_text(question, answer)
        lines = formatted_text.splitlines()

        if target.target_type == AnswerTargetType.PARAGRAPH_PLACEHOLDER:
            self._write_paragraph_placeholder(target.paragraph_obj, lines, target.placeholder_text)

        elif target.target_type == AnswerTargetType.PARAGRAPH_BOX:
            self._write_paragraph_box(target.paragraph_obj, lines)

        elif target.target_type in (
            AnswerTargetType.TABLE_CELL_COLUMN,
            AnswerTargetType.TABLE_CELL_ROW,
            AnswerTargetType.TABLE_CELL_BLANK,
        ):
            self._write_table_cell(target.cell_obj, lines, target.placeholder_text)

        elif target.target_type == AnswerTargetType.TABLE_CELL_APPEND:
            self._write_table_cell_append(target.cell_obj, lines)

        elif target.target_type == AnswerTargetType.TEXT_BOX:
            self._write_text_box(doc, target.txbx_element, lines)

        elif target.target_type == AnswerTargetType.CONTENT_CONTROL:
            self._write_content_control(doc, target.sdt_element, lines)

        elif target.target_type == AnswerTargetType.NEW_PARAGRAPH_ANCHOR:
            self._write_new_paragraph_anchor(target.paragraph_obj, lines)

        else:
            raise WorksheetFillingError(f"Unsupported target type: {target.target_type}")

        # If MCQ, bold the selected option in the question block if options were separate paragraphs
        if question.question_type == QuestionType.MCQ and answer.selected_option:
            self._highlight_mcq_option(doc, question, answer.selected_option)

    def _format_answer_text(self, question: ParsedQuestion, answer: GeneratedAnswer) -> str:
        """Format answer text cleanly based on classification."""
        text = answer.answer_text.strip()

        # Remove redundant leading "Answer:" or "Ans:" from answer engine output if present
        text = re.sub(r"^(?:Answer|Ans|Solution)\s*[:\-–—]\s*", "", text, flags=re.IGNORECASE).strip()

        if question.question_type == QuestionType.MCQ and answer.selected_option:
            opt_key = answer.selected_option.upper()
            # If answer text doesn't already include option letter prefix
            if not text.upper().startswith(opt_key):
                return f"{opt_key}. {text}"
        return text

    def _write_paragraph_placeholder(
        self,
        p: Paragraph,
        lines: List[str],
        placeholder_text: Optional[str],
    ) -> None:
        """Write into an existing placeholder paragraph (e.g. 'Answer: ')."""
        p_text = (placeholder_text or p.text).strip()

        # Clear existing text / runs
        p.text = ""

        # Check if the placeholder contained a label like "Answer:"
        has_label = bool(re.match(r"^(?:Answer|Ans|Solution|Response|Output)\s*[:\-–—]?", p_text, re.IGNORECASE))
        label_str = "Answer: " if has_label else ""

        if label_str:
            lbl_run = p.add_run(label_str)
            lbl_run.bold = True
            lbl_run.font.color.rgb = ANSWER_COLOR_RGB
            lbl_run.font.size = Pt(10.5)

        if lines:
            txt_run = p.add_run(lines[0])
            txt_run.font.color.rgb = ANSWER_COLOR_RGB
            txt_run.font.size = Pt(10.5)

        # For multi-line responses (long answer), insert continuation paragraphs immediately following
        current_anchor = p
        parent_doc = p._parent
        for line in lines[1:]:
            if not line.strip():
                continue
            new_p_elm = current_anchor._p.getparent()._new_p()
            current_anchor._p.addnext(new_p_elm)
            cont_p = Paragraph(new_p_elm, parent_doc)
            r = cont_p.add_run(line)
            if re.match(r"^\d+[\.\)]\s+[A-Za-z]", line.strip()):
                r.bold = True
            r.font.color.rgb = ANSWER_COLOR_RGB
            r.font.size = Pt(10.5)
            current_anchor = cont_p

    def _write_paragraph_box(self, p: Paragraph, lines: List[str]) -> None:
        """Write into an empty bordered paragraph box."""
        p.text = ""
        lbl_run = p.add_run("Answer: ")
        lbl_run.bold = True
        lbl_run.font.color.rgb = ANSWER_COLOR_RGB
        lbl_run.font.size = Pt(10.5)

        if lines:
            txt_run = p.add_run(lines[0])
            txt_run.font.color.rgb = ANSWER_COLOR_RGB
            txt_run.font.size = Pt(10.5)

        current_anchor = p
        parent_doc = p._parent
        for line in lines[1:]:
            if not line.strip():
                continue
            new_p_elm = current_anchor._p.getparent()._new_p()
            current_anchor._p.addnext(new_p_elm)
            cont_p = Paragraph(new_p_elm, parent_doc)
            r = cont_p.add_run(line)
            r.font.color.rgb = ANSWER_COLOR_RGB
            r.font.size = Pt(10.5)
            current_anchor = cont_p

    def _write_table_cell(self, cell: _Cell, lines: List[str], placeholder_text: Optional[str]) -> None:
        """Write into a dedicated table cell."""
        p = cell.paragraphs[0] if cell.paragraphs else cell.add_paragraph()
        p.text = ""

        # Check if cell had an "Answer:" label
        if placeholder_text and re.match(r"^(?:Answer|Ans|Solution)\s*[:\-–—]?", placeholder_text, re.IGNORECASE):
            lbl_run = p.add_run("Answer: ")
            lbl_run.bold = True
            lbl_run.font.color.rgb = ANSWER_COLOR_RGB
            lbl_run.font.size = Pt(10)

        if lines:
            txt_run = p.add_run(lines[0])
            txt_run.font.color.rgb = ANSWER_COLOR_RGB
            txt_run.font.size = Pt(10)

        # Multi-paragraph inside the same cell
        for line in lines[1:]:
            if not line.strip():
                continue
            new_p = cell.add_paragraph()
            r = new_p.add_run(line)
            if re.match(r"^\d+[\.\)]\s+[A-Za-z]", line.strip()):
                r.bold = True
            r.font.color.rgb = ANSWER_COLOR_RGB
            r.font.size = Pt(10)

    def _write_table_cell_append(self, cell: _Cell, lines: List[str]) -> None:
        """Append answer inside question cell when no separate cell exists."""
        ans_p = cell.add_paragraph()
        lbl_run = ans_p.add_run("Answer: ")
        lbl_run.bold = True
        lbl_run.font.color.rgb = ANSWER_COLOR_RGB
        lbl_run.font.size = Pt(10)

        if lines:
            txt_run = ans_p.add_run(lines[0])
            txt_run.font.color.rgb = ANSWER_COLOR_RGB
            txt_run.font.size = Pt(10)

        for line in lines[1:]:
            if not line.strip():
                continue
            new_p = cell.add_paragraph()
            r = new_p.add_run(line)
            r.font.color.rgb = ANSWER_COLOR_RGB
            r.font.size = Pt(10)

    def _write_text_box(self, doc: docx.Document, txbx_element: Any, lines: List[str]) -> None:
        """Write answer into w:txbxContent element in DrawingML or VML."""
        p_elems = txbx_element.findall("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
        if not p_elems:
            new_p_elm = doc._body._element._new_p()
            txbx_element.append(new_p_elm)
            p_elems = [new_p_elm]

        first_p = Paragraph(p_elems[0], doc)
        first_p.text = ""
        lbl_run = first_p.add_run("Answer: ")
        lbl_run.bold = True
        lbl_run.font.color.rgb = ANSWER_COLOR_RGB
        lbl_run.font.size = Pt(10)

        if lines:
            txt_run = first_p.add_run(lines[0])
            txt_run.font.color.rgb = ANSWER_COLOR_RGB
            txt_run.font.size = Pt(10)

        for line in lines[1:]:
            if not line.strip():
                continue
            new_p_elm = doc._body._element._new_p()
            txbx_element.append(new_p_elm)
            cont_p = Paragraph(new_p_elm, doc)
            r = cont_p.add_run(line)
            r.font.color.rgb = ANSWER_COLOR_RGB
            r.font.size = Pt(10)

    def _write_content_control(self, doc: docx.Document, sdt_element: Any, lines: List[str]) -> None:
        """Write answer into w:sdtContent structured document tag."""
        p_elems = sdt_element.findall(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
        if not p_elems:
            new_p_elm = doc._body._element._new_p()
            sdt_element.append(new_p_elm)
            p_elems = [new_p_elm]

        first_p = Paragraph(p_elems[0], doc)
        first_p.text = ""
        lbl_run = first_p.add_run("Answer: ")
        lbl_run.bold = True
        lbl_run.font.color.rgb = ANSWER_COLOR_RGB
        lbl_run.font.size = Pt(10.5)

        if lines:
            txt_run = first_p.add_run(lines[0])
            txt_run.font.color.rgb = ANSWER_COLOR_RGB
            txt_run.font.size = Pt(10.5)

        current_anchor = first_p
        for line in lines[1:]:
            if not line.strip():
                continue
            new_p_elm = doc._body._element._new_p()
            sdt_element.append(new_p_elm)
            cont_p = Paragraph(new_p_elm, doc)
            r = cont_p.add_run(line)
            r.font.color.rgb = ANSWER_COLOR_RGB
            r.font.size = Pt(10.5)

    def _write_new_paragraph_anchor(self, anchor_p: Paragraph, lines: List[str]) -> None:
        """Insert new paragraph immediately following anchor paragraph."""
        parent_doc = anchor_p._parent
        current_anchor = anchor_p

        new_p_elm = anchor_p._p.getparent()._new_p()
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

        for line in lines[1:]:
            if not line.strip():
                continue
            line_elm = current_anchor._p.getparent()._new_p()
            current_anchor._p.addnext(line_elm)
            line_p = Paragraph(line_elm, parent_doc)

            run = line_p.add_run(line)
            if re.match(r"^\d+\.\s+[A-Za-z]", line.strip()):
                run.bold = True
            run.font.color.rgb = ANSWER_COLOR_RGB
            run.font.size = Pt(10.5)
            current_anchor = line_p

    def _highlight_mcq_option(self, doc: docx.Document, question: ParsedQuestion, selected_option: str) -> None:
        """Bold and highlight matching MCQ option in document paragraphs if separated."""
        q_p_idx = question.source_location.get("paragraph_index")
        if q_p_idx is None or q_p_idx >= len(doc.paragraphs):
            return

        opt_prefix = selected_option.upper()
        # Search paragraphs following question for the option
        for idx in range(q_p_idx + 1, min(q_p_idx + 8, len(doc.paragraphs))):
            p = doc.paragraphs[idx]
            t = p.text.strip().upper()
            if re.match(rf"^(?:\({opt_prefix}\)|\[{opt_prefix}\]|{opt_prefix}[\.\)])\s+", t):
                for run in p.runs:
                    run.bold = True
                    run.font.color.rgb = ANSWER_COLOR_RGB
                break
