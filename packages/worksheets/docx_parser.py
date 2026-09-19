"""DOCX Worksheet Parser using python-docx.

Extracts structured sections, questions, options, tables, and formatting metadata
from Microsoft Word (.docx) worksheet documents in preserved sequential order.
"""

import re
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import docx
from docx.text.paragraph import Paragraph
from docx.table import Table

from packages.worksheets.answer_target import is_answer_placeholder_text
from packages.worksheets.base_parser import BaseWorksheetParser
from packages.worksheets.classifier import QuestionClassifier
from packages.worksheets.models import (
    ParsedQuestion,
    ParsedWorksheet,
    QuestionOption,
    QuestionType,
    WorksheetSection,
)


def _roman_to_int(roman: str) -> Optional[int]:
    """Convert Roman numeral string to integer."""
    roman_map = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    val = 0
    prev = 0
    for ch in reversed(roman.upper().strip()):
        curr = roman_map.get(ch, 0)
        if curr == 0:
            return None
        if curr >= prev:
            val += curr
        else:
            val -= curr
        prev = curr
    return val if val > 0 else None


def _int_to_letter(n: int) -> str:
    """Convert 1-based integer to alphabetical letter (1 -> A, 2 -> B, etc.)."""
    res = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        res = chr(65 + remainder) + res
    return res


def _int_to_roman(n: int) -> str:
    """Convert integer to Roman numeral."""
    val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syb = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]
    roman_num = ""
    i = 0
    while n > 0 and i < len(val):
        for _ in range(n // val[i]):
            roman_num += syb[i]
            n -= val[i]
        i += 1
    return roman_num


class NumberingInfo:
    """Holds parsed list numbering metadata for a paragraph."""

    def __init__(
        self,
        is_numbered: bool,
        number_str: str = "",
        rendered_prefix: str = "",
        num_fmt: str = "",
        num_id: str = "",
        ilvl: str = "0",
    ):
        self.is_numbered = is_numbered
        self.number_str = number_str
        self.rendered_prefix = rendered_prefix
        self.num_fmt = num_fmt
        self.num_id = num_id
        self.ilvl = ilvl


class DocxNumberingResolver:
    """Resolves Word list numbering (w:numPr) for paragraphs into rendered prefixes."""

    W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def __init__(self, doc: docx.Document):
        self.doc = doc
        self.num_dict: Dict[str, str] = {}
        self.abstract_dict: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.counters: Dict[Tuple[str, str], int] = {}
        self._load_numbering_part()

    def _load_numbering_part(self) -> None:
        if not hasattr(self.doc.part, "numbering_part") or self.doc.part.numbering_part is None:
            return
        try:
            num_part_el = self.doc.part.numbering_part._element
            W = self.W_NS
            for num in num_part_el.iter(f"{W}num"):
                num_id = num.get(f"{W}numId")
                abstract_elm = num.find(f"{W}abstractNumId")
                if num_id and abstract_elm is not None:
                    self.num_dict[num_id] = abstract_elm.get(f"{W}val")

            for abs_num in num_part_el.iter(f"{W}abstractNum"):
                abs_id = abs_num.get(f"{W}abstractNumId")
                if not abs_id:
                    continue
                lvls: Dict[str, Dict[str, Any]] = {}
                for lvl in abs_num.iter(f"{W}lvl"):
                    ilvl = lvl.get(f"{W}ilvl") or "0"
                    num_fmt_elm = lvl.find(f"{W}numFmt")
                    num_fmt = num_fmt_elm.get(f"{W}val") if num_fmt_elm is not None else "decimal"
                    lvl_text_elm = lvl.find(f"{W}lvlText")
                    lvl_text = lvl_text_elm.get(f"{W}val") if lvl_text_elm is not None else "%1."
                    start_elm = lvl.find(f"{W}start")
                    start = int(start_elm.get(f"{W}val")) if (start_elm is not None and str(start_elm.get(f"{W}val", "")).isdigit()) else 1
                    lvls[ilvl] = {"numFmt": num_fmt, "lvlText": lvl_text, "start": start}
                self.abstract_dict[abs_id] = lvls
        except Exception:
            pass

    def get_numbering_info(self, paragraph: Paragraph) -> Optional[NumberingInfo]:
        pPr = paragraph._p.pPr
        if pPr is None:
            return None
        numPr = pPr.numPr
        if numPr is None:
            return None

        W = self.W_NS
        num_id_elm = numPr.numId
        ilvl_elm = numPr.ilvl

        num_id = num_id_elm.get(f"{W}val") if num_id_elm is not None else None
        if not num_id:
            return None
        ilvl = ilvl_elm.get(f"{W}val") if ilvl_elm is not None else "0"

        abs_id = self.num_dict.get(num_id)
        lvl_info = self.abstract_dict.get(abs_id, {}).get(ilvl) if abs_id else None

        num_fmt = lvl_info.get("numFmt", "decimal").lower() if lvl_info else "decimal"
        lvl_text = lvl_info.get("lvlText", "%1.") if lvl_info else "%1."
        start = lvl_info.get("start", 1) if lvl_info else 1

        # Check for bullets
        if num_fmt == "bullet" or any(ch in lvl_text for ch in ["\uf0b7", "\uf0a7", "\u2022", "•"]):
            return NumberingInfo(is_numbered=False, num_fmt="bullet", num_id=num_id, ilvl=ilvl)

        key = (num_id, ilvl)
        if key not in self.counters:
            self.counters[key] = start
        else:
            self.counters[key] += 1

        current_val = self.counters[key]

        if num_fmt in ("decimal", "decimalzero", "ordinal", "cardinaltext", "none"):
            num_str = str(current_val)
        elif num_fmt == "lowerletter":
            num_str = _int_to_letter(current_val).lower()
        elif num_fmt == "upperletter":
            num_str = _int_to_letter(current_val).upper()
        elif num_fmt == "lowerroman":
            num_str = _int_to_roman(current_val).lower()
        elif num_fmt == "upperroman":
            num_str = _int_to_roman(current_val).upper()
        else:
            num_str = str(current_val)

        ilvl_int = int(ilvl) if ilvl.isdigit() else 0
        placeholder = f"%{ilvl_int + 1}"
        if placeholder in lvl_text:
            rendered = lvl_text.replace(placeholder, num_str)
        else:
            rendered = f"{num_str}."

        return NumberingInfo(
            is_numbered=True,
            number_str=str(current_val) if num_fmt in ("decimal", "decimalzero") else num_str,
            rendered_prefix=rendered.strip(),
            num_fmt=num_fmt,
            num_id=num_id,
            ilvl=ilvl,
        )


class DocxWorksheetParser(BaseWorksheetParser):
    """Parser for DOCX worksheet files preserving order, structure, and formatting."""

    # Regex patterns for question detection
    QUESTION_START_PATTERNS = [
        # Activity with emoji / bullet (e.g. 🔹 1. Role-Play Simulation)
        re.compile(r"^[🔹🔸■•*►-]?\s*(?:Activity\s*)?(\d+)[\.\):]\s*(.+)", re.DOTALL),
        # Question with Q/Question prefix (e.g. Q1. What is..., Question 2:)
        re.compile(r"^Q(?:uestion)?\s*(\d+)[\.\):]\s*(.+)", re.IGNORECASE | re.DOTALL),
        # Standard numbering (e.g. 1. What is..., 1) Explain...)
        re.compile(r"^(\d+)[\.\)]\s+(.+)", re.DOTALL),
        # Bracketed numbering (e.g. [1] Define..., (1) State...)
        re.compile(r"^[\(\[](\d+)[\)\]]\s+(.+)", re.DOTALL),
    ]

    # Option detection patterns (e.g. A. ..., a) ..., (A) ..., [B] ...)
    OPTION_PATTERNS = [
        re.compile(r"^(?:\(([A-Ea-e])\)|\[([A-Ea-e])\]|([A-Ea-e])[\.\)])\s+(.*)", re.DOTALL),
    ]

    # Section header patterns
    SECTION_PATTERNS = [
        re.compile(r"^(?:Part|Section)\s*([A-Za-z0-9]+)[\s:\-–—]*(.*)", re.IGNORECASE),
        re.compile(r"^(?:Unit\s*([IVXLCDM\d]+))[\s:\-–—]*(.*)", re.IGNORECASE),
        re.compile(r"^(?:SLO\s*(\d+))[\s:\-–—]*(.*)", re.IGNORECASE),
        re.compile(r"^(?:Session\s*(\d+))[\s:\-–—]*(.*)", re.IGNORECASE),
    ]

    # Metadata extraction patterns (Unit, Session, SLO)
    UNIT_SESSION_SLO_PATTERN = re.compile(
        r"Unit\s*([IVXLCDM\d]+)[\s\-–—]+Session\s*(\d+)[\s\-–—]+SLO\s*(\d+)",
        re.IGNORECASE,
    )
    COURSE_CODE_PATTERN = re.compile(r"\b([0-9]{2}[A-Z]{3}[0-9]{3}[A-Z]?)\b")

    def _iter_block_items(self, parent: Any) -> Generator[Any, None, None]:
        """Yield each paragraph and table in exact sequential document order."""
        if hasattr(parent, "_element") and hasattr(parent._element, "body"):
            body = parent._element.body
        else:
            body = getattr(parent, "_element", parent)

        for child in body.iterchildren():
            if child.tag.endswith("p"):
                yield Paragraph(child, parent)
            elif child.tag.endswith("tbl"):
                yield Table(child, parent)

    def _extract_formatting(self, paragraph: Paragraph) -> Dict[str, Any]:
        """Extract font styles, runs, and paragraph styling for reconstruction."""
        runs_info = []
        for run in paragraph.runs:
            if run.text:
                runs_info.append({
                    "text": run.text,
                    "bold": bool(run.bold),
                    "italic": bool(run.italic),
                    "underline": bool(run.underline),
                    "font_name": run.font.name if run.font else None,
                })
        return {
            "style_name": paragraph.style.name if paragraph.style else "Normal",
            "alignment": str(paragraph.alignment) if paragraph.alignment is not None else None,
            "runs": runs_info,
        }

    def _extract_metadata(self, doc: docx.Document) -> Dict[str, Any]:
        """Extract course code, unit, session, and SLO metadata from header paragraphs."""
        metadata: Dict[str, Any] = {}
        for p in doc.paragraphs[:10]:
            text = p.text.strip()
            if not text:
                continue

            # Check Unit - Session - SLO
            uss_match = self.UNIT_SESSION_SLO_PATTERN.search(text)
            if uss_match:
                unit_raw, session_raw, slo_raw = uss_match.groups()
                unit_val = _roman_to_int(unit_raw) if not unit_raw.isdigit() else int(unit_raw)
                metadata["unit"] = unit_val or unit_raw
                metadata["session"] = int(session_raw)
                metadata["slo"] = int(slo_raw)
                metadata["header_text"] = text

            # Check Course Code
            code_match = self.COURSE_CODE_PATTERN.search(text)
            if code_match and "course_code" not in metadata:
                metadata["course_code"] = code_match.group(1)

            # Check individual SLO / Session / Unit if composite pattern was not present
            if "slo" not in metadata:
                slo_match = re.search(r"\bSLO\s*[:\-–—]?\s*(\d+)", text, re.IGNORECASE)
                if slo_match:
                    metadata["slo"] = int(slo_match.group(1))
            if "session" not in metadata:
                sess_match = re.search(r"\bSession\s*[:\-–—]?\s*(\d+)", text, re.IGNORECASE)
                if sess_match:
                    metadata["session"] = int(sess_match.group(1))
            if "unit" not in metadata:
                unit_match = re.search(r"\bUnit\s*[:\-–—]?\s*([IVXLCDM\d]+)", text, re.IGNORECASE)
                if unit_match:
                    u_val = unit_match.group(1)
                    metadata["unit"] = _roman_to_int(u_val) if not u_val.isdigit() else int(u_val)

        return metadata

    def parse(self, file_path: Path) -> ParsedWorksheet:
        """Parse DOCX document into a structured ParsedWorksheet."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Worksheet file not found: {path}")

        # Open in read-only fashion (never calls doc.save)
        doc = docx.Document(str(path))

        metadata = self._extract_metadata(doc)
        sections: List[WorksheetSection] = []
        questions: List[ParsedQuestion] = []

        current_section: Optional[WorksheetSection] = None
        current_question: Optional[ParsedQuestion] = None
        source_order = 0
        paragraph_count = 0
        table_count = 0

        numbering_resolver = DocxNumberingResolver(doc)

        for block_idx, block in enumerate(self._iter_block_items(doc)):
            if isinstance(block, Paragraph):
                paragraph_count += 1
                text = block.text.strip()
                if not text:
                    continue

                # Ignore top-level metadata headers as sections
                if self.UNIT_SESSION_SLO_PATTERN.search(text) or (
                    ("course code" in text.lower() or "course title" in text.lower()) and not current_question
                ):
                    continue

                num_info = numbering_resolver.get_numbering_info(block)
                effective_text = text
                if num_info and num_info.is_numbered:
                    if not re.match(r"^\d+[\.\)]", text):
                        effective_text = f"{num_info.rendered_prefix} {text}"

                # 1. Check for Section Headers
                # Either explicit Heading style or Section regex
                style_name = (block.style.name if block.style else "").lower()
                is_heading_style = "heading" in style_name
                sec_match = None
                for sp in self.SECTION_PATTERNS:
                    sec_match = sp.search(text)
                    if sec_match:
                        break

                if (is_heading_style and not self._match_question_start(effective_text)) or (sec_match and len(text) < 100):
                    # Finalize current question if open
                    if current_question:
                        self._finalize_question(current_question)
                        questions.append(current_question)
                        current_question = None

                    sec_name = text
                    current_section = WorksheetSection(
                        name=sec_name,
                        title=sec_name,
                        start_order=source_order + 1,
                    )
                    sections.append(current_section)
                    continue

                # 2. Check for Option if a question is currently open
                if current_question:
                    opt_match = self._match_option(effective_text)
                    if opt_match:
                        opt_key, opt_text = opt_match
                        current_question.options.append(QuestionOption(key=opt_key, text=opt_text))
                        continue

                    # Check if this paragraph is an explicit answer placeholder
                    if is_answer_placeholder_text(text):
                        current_question.source_location["answer_placeholder_paragraph_index"] = block_idx
                        current_question.formatting_metadata["answer_placeholder_text"] = text
                        continue

                    # Check for Activity / Deliverable sub-parts (like in 1011.docx)
                    if any(text.startswith(prefix) for prefix in [
                        "🧩 Activity:", "Activity:", "🎯 Learning Objective:",
                        "Learning Objective:", "📌 Solution/Outcome:", "Solution/Outcome:",
                        "💡 Outcome:", "Outcome:", "Deliverable:", "Requirements:"
                    ]):
                        # Append activity context / sub-part
                        if current_question.context_or_activity:
                            current_question.context_or_activity += f"\n{text}"
                        else:
                            current_question.context_or_activity = text
                        continue

                # 3. Check for New Question Start
                q_match = self._match_question_start(effective_text)
                if q_match:
                    if current_question:
                        self._finalize_question(current_question)
                        questions.append(current_question)

                    source_order += 1
                    q_num, q_body = q_match

                    # Check for inline options
                    clean_body, inline_opts = QuestionClassifier.extract_inline_options(q_body)
                    extracted_marks = QuestionClassifier.extract_marks(clean_body)

                    formatting_meta = self._extract_formatting(block)
                    if num_info and num_info.is_numbered:
                        formatting_meta["list_numbering"] = {
                            "num_id": num_info.num_id,
                            "ilvl": num_info.ilvl,
                            "number_str": num_info.number_str,
                            "rendered_prefix": num_info.rendered_prefix,
                        }

                    current_question = ParsedQuestion(
                        question_id=f"q_{source_order}_{q_num}",
                        question_number=str(q_num),
                        question_text=clean_body,
                        options=inline_opts,
                        marks=extracted_marks,
                        section=current_section.name if current_section else None,
                        source_order=source_order,
                        source_location={"paragraph_index": block_idx},
                        formatting_metadata=formatting_meta,
                    )
                    continue

                # 4. Continuation of Current Question or Unclassified Text
                if current_question:
                    # If this is not metadata / top header, treat as continuation of question description
                    current_question.question_text += f"\n{text}"
                else:
                    # Paragraph before any numbered question: could be standalone conceptual question or instruction
                    QUESTION_VERBS = (
                        "define", "explain", "state", "discuss", "write", "demonstrate",
                        "give", "implement", "describe", "illustrate", "differentiate",
                        "distinguish", "compare", "list", "develop", "design", "calculate",
                        "compute", "solve", "construct", "analyze", "create", "show"
                    )
                    is_question_prompt = "?" in text or any(
                        re.match(rf"^\b{kw}\b", text.strip(), re.IGNORECASE) for kw in QUESTION_VERBS
                    )
                    if is_question_prompt and len(text.strip()) > 10:
                        source_order += 1
                        extracted_marks = QuestionClassifier.extract_marks(text)
                        clean_text, inline_opts = QuestionClassifier.extract_inline_options(text)
                        current_question = ParsedQuestion(
                            question_id=f"q_{source_order}",
                            question_number=str(source_order),
                            question_text=clean_text,
                            options=inline_opts,
                            marks=extracted_marks,
                            section=current_section.name if current_section else None,
                            source_order=source_order,
                            source_location={"paragraph_index": block_idx},
                            formatting_metadata=self._extract_formatting(block),
                        )

            elif isinstance(block, Table):
                table_count += 1
                # Finalize any pending paragraph question
                if current_question:
                    self._finalize_question(current_question)
                    questions.append(current_question)
                    current_question = None

                # Process table for questions
                table_questions, is_q_table = self._parse_table_questions(
                    block, table_idx=table_count - 1, start_order=source_order + 1, section=current_section
                )
                if is_q_table and table_questions:
                    for tq in table_questions:
                        source_order += 1
                        tq.source_order = source_order
                        questions.append(tq)
                elif questions:
                    # If table is supplementary to the preceding question, attach it
                    tbl_md = self._table_to_markdown(block)
                    questions[-1].formatting_metadata["supplementary_table"] = tbl_md
                    questions[-1].question_text += f"\n\n{tbl_md}"

        # Finalize last open question
        if current_question:
            self._finalize_question(current_question)
            questions.append(current_question)

        title = metadata.get("header_text") or (sections[0].name if sections else path.stem)

        return ParsedWorksheet(
            filename=path.name,
            file_format="docx",
            title=title,
            course_code=metadata.get("course_code"),
            unit=metadata.get("unit"),
            session=metadata.get("session"),
            slo=metadata.get("slo"),
            metadata=metadata,
            sections=sections,
            questions=questions,
            raw_document_info={
                "paragraph_count": paragraph_count,
                "table_count": table_count,
                "total_questions": len(questions),
            },
        )

    def _match_question_start(self, text: str) -> Optional[Tuple[str, str]]:
        """Check if text starts a question, returning (question_number, question_body)."""
        for pattern in self.QUESTION_START_PATTERNS:
            match = pattern.match(text)
            if match:
                q_num = match.group(1).strip()
                q_body = match.group(2).strip()
                # Don't treat a single letter option (e.g. A. B.) as question start
                return q_num, q_body
        return None

    def _match_option(self, text: str) -> Optional[Tuple[str, str]]:
        """Check if text represents an option (e.g. A. ..., (b) ...)."""
        for pattern in self.OPTION_PATTERNS:
            match = pattern.match(text)
            if match:
                opt_key = (match.group(1) or match.group(2) or match.group(3)).upper()
                opt_text = match.group(4).strip()
                return opt_key, opt_text
        return None

    def _finalize_question(self, question: ParsedQuestion) -> None:
        """Classify and polish question before adding to question list."""
        question.question_type = QuestionClassifier.classify(question)

    def _parse_table_questions(
        self,
        table: Table,
        table_idx: int,
        start_order: int,
        section: Optional[WorksheetSection],
    ) -> Tuple[List[ParsedQuestion], bool]:
        """Inspect table to determine if it is a question table and extract questions."""
        if not table.rows:
            return [], False

        # Inspect header row
        header_cells = [c.text.strip().lower() for c in table.rows[0].cells]
        q_col_idx = -1
        num_col_idx = -1
        marks_col_idx = -1
        ans_col_idx = -1

        for idx, h in enumerate(header_cells):
            if any(term in h for term in ["question", "problem", "statement", "task"]):
                q_col_idx = idx
            elif any(term in h for term in ["s.no", "q.no", "sl.no", "no", "item"]):
                num_col_idx = idx
            elif any(term in h for term in ["marks", "mark", "max marks", "pts"]):
                marks_col_idx = idx
            elif any(term in h for term in ["answer", "solution", "response", "output", "result"]):
                ans_col_idx = idx

        start_r = 1
        if q_col_idx == -1:
            for test_row in table.rows:
                for c_idx, cell in enumerate(test_row.cells):
                    txt = cell.text.strip()
                    if self._match_question_start(txt):
                        q_col_idx = c_idx
                        break
                if q_col_idx != -1:
                    break
            if q_col_idx != -1:
                start_r = 0
                if q_col_idx + 1 < len(table.rows[0].cells):
                    ans_col_idx = q_col_idx + 1

        # If question column is identified, this is a questions table
        if q_col_idx != -1:
            extracted: List[ParsedQuestion] = []
            for r_idx, row in enumerate(table.rows[start_r:], start=start_r):
                if q_col_idx >= len(row.cells):
                    continue
                q_text = row.cells[q_col_idx].text.strip()
                if not q_text:
                    continue

                q_num = str(r_idx)
                if num_col_idx != -1 and num_col_idx < len(row.cells):
                    val = row.cells[num_col_idx].text.strip()
                    if val:
                        q_num = val

                marks = None
                if marks_col_idx != -1 and marks_col_idx < len(row.cells):
                    raw_val = row.cells[marks_col_idx].text.strip()
                    marks = QuestionClassifier.extract_marks(raw_val)
                    if marks is None and raw_val:
                        try:
                            marks = float(raw_val)
                        except ValueError:
                            pass
                if marks is None:
                    marks = QuestionClassifier.extract_marks(q_text)

                clean_text, inline_opts = QuestionClassifier.extract_inline_options(q_text)

                source_loc = {
                    "table_index": table_idx,
                    "row_index": r_idx,
                    "col_index": q_col_idx,
                }
                if ans_col_idx != -1:
                    source_loc["answer_col_index"] = ans_col_idx

                pq = ParsedQuestion(
                    question_id=f"tbl_{table_idx}_r{r_idx}_{q_num}",
                    question_number=q_num,
                    question_text=clean_text,
                    options=inline_opts,
                    marks=marks,
                    section=section.name if section else None,
                    source_order=start_order + len(extracted),
                    source_location=source_loc,
                    formatting_metadata={"table_row": True},
                )
                self._finalize_question(pq)
                extracted.append(pq)
            return extracted, True

        return [], False

    def _table_to_markdown(self, table: Table) -> str:
        """Convert a table to Markdown format for preservation in question context."""
        md_lines = []
        for r_idx, row in enumerate(table.rows):
            # deduplicate merged cells in row
            row_texts = []
            for cell in row.cells:
                txt = cell.text.strip().replace("\n", " ")
                if not row_texts or row_texts[-1] != txt:
                    row_texts.append(txt)
            md_lines.append("| " + " | ".join(row_texts) + " |")
            if r_idx == 0:
                md_lines.append("| " + " | ".join(["---"] * len(row_texts)) + " |")
        return "\n".join(md_lines)
