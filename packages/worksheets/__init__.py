"""Worksheet processing, parsing, answering, and document filling package.

Provides domain models, classifier, format parsers (DOCX, PDF), pluggable
answer generation engines, non-destructive document filler, and end-to-end pipeline.
"""

from packages.worksheets.answer_engine import (
    AnswerEngineFactory,
    BaseAnswerEngine,
    LLMAnswerEngine,
    RuleBasedAnswerEngine,
)
from packages.worksheets.answer_models import (
    AnswerStatus,
    GeneratedAnswer,
    PipelineResult,
    WorksheetAnswers,
)
from packages.worksheets.base_parser import BaseWorksheetParser
from packages.worksheets.classifier import QuestionClassifier
from packages.worksheets.docx_parser import DocxWorksheetParser
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.inspector import format_worksheet_summary, inspect_worksheet
from packages.worksheets.models import (
    ParsedQuestion,
    ParsedWorksheet,
    QuestionOption,
    QuestionType,
    WorksheetSection,
)
from packages.worksheets.parser import WorksheetParser
from packages.worksheets.pdf_parser import PdfWorksheetParser
from packages.worksheets.pipeline import WorksheetPipeline
from packages.worksheets.processor import DefaultWorksheetProcessor, WorksheetProcessor

__all__ = [
    "BaseWorksheetParser",
    "DocxWorksheetParser",
    "PdfWorksheetParser",
    "WorksheetParser",
    "QuestionClassifier",
    "ParsedQuestion",
    "ParsedWorksheet",
    "QuestionOption",
    "QuestionType",
    "WorksheetSection",
    "WorksheetProcessor",
    "DefaultWorksheetProcessor",
    "format_worksheet_summary",
    "inspect_worksheet",
    "AnswerStatus",
    "GeneratedAnswer",
    "WorksheetAnswers",
    "PipelineResult",
    "BaseAnswerEngine",
    "RuleBasedAnswerEngine",
    "LLMAnswerEngine",
    "AnswerEngineFactory",
    "DocxWorksheetFiller",
    "WorksheetPipeline",
]
