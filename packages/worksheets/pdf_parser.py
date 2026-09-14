"""PDF Worksheet Parser using pypdf.

Extracts text, page numbers, question ordering, sections, options, and metadata
from PDF worksheet documents while maintaining a clean separation from DOCX parsing.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader

from packages.worksheets.base_parser import BaseWorksheetParser
from packages.worksheets.classifier import QuestionClassifier
from packages.worksheets.models import (
    ParsedQuestion,
    ParsedWorksheet,
    QuestionOption,
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


class PdfWorksheetParser(BaseWorksheetParser):
    """Parser for PDF worksheet documents extracting questions and page locations."""

    QUESTION_START_PATTERNS = [
        re.compile(r"^[🔹🔸■•*►-]?\s*(?:Activity\s*)?(\d+)[\.\):]\s*(.+)", re.DOTALL),
        re.compile(r"^Q(?:uestion)?\s*(\d+)[\.\):]\s*(.+)", re.IGNORECASE | re.DOTALL),
        re.compile(r"^(\d+)[\.\)]\s+(.+)", re.DOTALL),
        re.compile(r"^[\(\[](\d+)[\)\]]\s+(.+)", re.DOTALL),
    ]

    OPTION_PATTERNS = [
        re.compile(r"^(?:\(([A-Ea-e])\)|\[([A-Ea-e])\]|([A-Ea-e])[\.\)])\s+(.*)", re.DOTALL),
    ]

    SECTION_PATTERNS = [
        re.compile(r"^(?:Part|Section)\s*([A-Za-z0-9]+)[\s:\-–—]*(.*)", re.IGNORECASE),
        re.compile(r"^(?:Unit\s*([IVXLCDM\d]+))[\s:\-–—]*(.*)", re.IGNORECASE),
    ]

    UNIT_SESSION_SLO_PATTERN = re.compile(
        r"Unit\s*([IVXLCDM\d]+)[\s\-–—]+Session\s*(\d+)[\s\-–—]+SLO\s*(\d+)",
        re.IGNORECASE,
    )
    COURSE_CODE_PATTERN = re.compile(r"\b([0-9]{2}[A-Z]{3}[0-9]{3}[A-Z]?)\b")

    def _extract_header_metadata(self, first_page_text: str) -> Dict[str, Any]:
        """Detect header metadata from initial page text."""
        metadata: Dict[str, Any] = {}
        match = self.UNIT_SESSION_SLO_PATTERN.search(first_page_text)
        if match:
            unit_raw, session_raw, slo_raw = match.groups()
            unit_val = _roman_to_int(unit_raw) if not unit_raw.isdigit() else int(unit_raw)
            metadata["unit"] = unit_val or unit_raw
            metadata["session"] = int(session_raw)
            metadata["slo"] = int(slo_raw)

        code_match = self.COURSE_CODE_PATTERN.search(first_page_text)
        if code_match:
            metadata["course_code"] = code_match.group(1)

        return metadata

    def parse(self, file_path: Path) -> ParsedWorksheet:
        """Parse PDF document into a structured ParsedWorksheet."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Worksheet PDF not found: {path}")

        reader = PdfReader(str(path))
        num_pages = len(reader.pages)

        first_page_text = reader.pages[0].extract_text() if num_pages > 0 else ""
        metadata = self._extract_header_metadata(first_page_text)

        sections: List[WorksheetSection] = []
        questions: List[ParsedQuestion] = []

        current_section: Optional[WorksheetSection] = None
        current_question: Optional[ParsedQuestion] = None
        source_order = 0

        for page_idx, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            lines = [line.strip() for line in page_text.splitlines() if line.strip()]

            for line_idx, line in enumerate(lines):
                # 1. Section Header Check
                sec_match = None
                for sp in self.SECTION_PATTERNS:
                    sec_match = sp.search(line)
                    if sec_match:
                        break

                if sec_match and len(line) < 100:
                    if current_question:
                        self._finalize_question(current_question)
                        questions.append(current_question)
                        current_question = None

                    current_section = WorksheetSection(
                        name=line,
                        title=line,
                        start_order=source_order + 1,
                    )
                    sections.append(current_section)
                    continue

                # 2. Check for Option if question open
                if current_question:
                    opt_match = self._match_option(line)
                    if opt_match:
                        opt_key, opt_text = opt_match
                        current_question.options.append(QuestionOption(key=opt_key, text=opt_text))
                        continue

                # 3. Check for New Question Start
                q_match = self._match_question_start(line)
                if q_match:
                    if current_question:
                        self._finalize_question(current_question)
                        questions.append(current_question)

                    source_order += 1
                    q_num, q_body = q_match
                    clean_body, inline_opts = QuestionClassifier.extract_inline_options(q_body)
                    extracted_marks = QuestionClassifier.extract_marks(clean_body)

                    current_question = ParsedQuestion(
                        question_id=f"pdf_p{page_idx}_q{source_order}_{q_num}",
                        question_number=str(q_num),
                        question_text=clean_body,
                        options=inline_opts,
                        marks=extracted_marks,
                        section=current_section.name if current_section else None,
                        source_order=source_order,
                        source_location={"page": page_idx, "line_index": line_idx},
                        formatting_metadata={"format": "pdf", "page": page_idx},
                    )
                    continue

                # 4. Continuation line
                if current_question:
                    current_question.question_text += f" {line}"

        if current_question:
            self._finalize_question(current_question)
            questions.append(current_question)

        title = metadata.get("header_text") or (sections[0].name if sections else path.stem)

        return ParsedWorksheet(
            filename=path.name,
            file_format="pdf",
            title=title,
            course_code=metadata.get("course_code"),
            unit=metadata.get("unit"),
            session=metadata.get("session"),
            slo=metadata.get("slo"),
            metadata=metadata,
            sections=sections,
            questions=questions,
            raw_document_info={
                "page_count": num_pages,
                "total_questions": len(questions),
            },
        )

    def _match_question_start(self, text: str) -> Optional[Tuple[str, str]]:
        """Check if text starts a question, returning (question_number, question_body)."""
        for pattern in self.QUESTION_START_PATTERNS:
            match = pattern.match(text)
            if match:
                return match.group(1).strip(), match.group(2).strip()
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
        """Classify question before appending."""
        question.question_type = QuestionClassifier.classify(question)
