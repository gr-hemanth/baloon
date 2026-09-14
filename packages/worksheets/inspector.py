"""Worksheet inspection utility for developers.

Parses any DOCX or PDF worksheet and outputs a structured summary of its
sections, question numbers, classified types, options, and metadata.

Usage:
    python -m packages.worksheets.inspect <path_to_worksheet>
"""

import io
import sys
from pathlib import Path
from typing import Optional

from packages.worksheets.models import ParsedWorksheet, QuestionType
from packages.worksheets.parser import WorksheetParser


def format_worksheet_summary(parsed: ParsedWorksheet) -> str:
    """Format a ParsedWorksheet into human-readable structured inspection text."""
    lines = []
    lines.append("=" * 60)
    lines.append("Worksheet:")
    lines.append(f"  filename: {parsed.filename}")
    lines.append(f"  format: {parsed.file_format.upper()}")
    if parsed.title:
        lines.append(f"  title: {parsed.title}")
    if parsed.course_code:
        lines.append(f"  course: {parsed.course_code}")
    if parsed.unit or parsed.session or parsed.slo:
        lines.append(f"  metadata: Unit={parsed.unit}, Session={parsed.session}, SLO={parsed.slo}")

    lines.append("")
    lines.append("Sections:")
    if parsed.sections:
        for sec in parsed.sections:
            lines.append(f"  - {sec.name}")
    else:
        lines.append("  (No explicit section headers)")

    lines.append("")
    lines.append("Questions:")
    if not parsed.questions:
        lines.append("  (No questions detected)")
    else:
        for idx, q in enumerate(parsed.questions, start=1):
            q_num_str = f"Q{q.question_number}" if q.question_number else f"{idx}."
            marks_str = f" [{int(q.marks) if q.marks.is_integer() else q.marks} Marks]" if q.marks else ""
            lines.append(f"  {idx}. [{q.question_type.value}] {q_num_str}: {q.question_text}{marks_str}")

            if q.options:
                for opt in q.options:
                    lines.append(f"     {opt.key}. {opt.text}")

            if q.context_or_activity:
                # Indent context
                for ctx_line in q.context_or_activity.splitlines():
                    if ctx_line.strip():
                        lines.append(f"     | {ctx_line.strip()}")

            lines.append("")

    # Summary statistics
    counts = {
        QuestionType.MCQ: len(parsed.get_questions_by_type(QuestionType.MCQ)),
        QuestionType.ONE_WORD: len(parsed.get_questions_by_type(QuestionType.ONE_WORD)),
        QuestionType.SHORT_ANSWER: len(parsed.get_questions_by_type(QuestionType.SHORT_ANSWER)),
        QuestionType.LONG_ANSWER: len(parsed.get_questions_by_type(QuestionType.LONG_ANSWER)),
        QuestionType.UNKNOWN: len(parsed.get_questions_by_type(QuestionType.UNKNOWN)),
    }
    lines.append("-" * 60)
    lines.append("Summary:")
    lines.append(f"  Total Questions: {parsed.question_count}")
    lines.append(f"  MCQ: {counts[QuestionType.MCQ]}")
    lines.append(f"  One-Word / Fill-in-Blank: {counts[QuestionType.ONE_WORD]}")
    lines.append(f"  Short Answer: {counts[QuestionType.SHORT_ANSWER]}")
    lines.append(f"  Long Answer: {counts[QuestionType.LONG_ANSWER]}")
    lines.append(f"  Unknown: {counts[QuestionType.UNKNOWN]}")
    lines.append("=" * 60)

    return "\n".join(lines)


def inspect_worksheet(file_path: Path) -> str:
    """Parse and return structured summary string for a worksheet."""
    parser = WorksheetParser()
    parsed = parser.parse(file_path)
    return format_worksheet_summary(parsed)


def main():
    # Ensure UTF-8 output across Windows consoles
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    if len(sys.argv) > 1:
        target_path = Path(sys.argv[1])
    else:
        # Default fallback to real_1011.docx in artifacts if available
        default_real = Path("artifacts/real_1011.docx")
        if default_real.exists():
            target_path = default_real
        else:
            print("Usage: python -m packages.worksheets.inspect <path_to_worksheet>")
            sys.exit(1)

    try:
        summary = inspect_worksheet(target_path)
        print(summary)
    except Exception as exc:
        print(f"Error inspecting worksheet {target_path}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
