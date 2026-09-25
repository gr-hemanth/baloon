"""Domain models for worksheet answer generation and document filling."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from packages.worksheets.models import ParsedWorksheet, QuestionType


class AnswerStatus(str, Enum):
    """Execution status of an answer generation attempt."""
    SUCCESS = "SUCCESS"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    ERROR = "ERROR"
    UNANSWERED = "UNANSWERED"


class GeneratedAnswer(BaseModel):
    """Structured answer generated for an individual question."""
    question_id: str
    question_number: Optional[str] = None
    question_type: QuestionType = QuestionType.UNKNOWN
    answer_text: str
    selected_option: Optional[str] = None  # e.g., "A", "B", "C", "D"
    confidence: float = 1.0  # 0.0 to 1.0
    explanation: Optional[str] = None
    target_answers: Dict[str, str] = Field(default_factory=dict)
    status: AnswerStatus = AnswerStatus.SUCCESS
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def is_reliable(self, threshold: float = 0.5) -> bool:
        """Check if the answer was generated with sufficient confidence."""
        return self.status == AnswerStatus.SUCCESS and self.confidence >= threshold


class WorksheetAnswers(BaseModel):
    """Collection of answers generated for a worksheet."""
    worksheet_filename: str
    answers: List[GeneratedAnswer] = Field(default_factory=list)
    provider: str = "default"
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def total_count(self) -> int:
        return len(self.answers)

    @property
    def success_count(self) -> int:
        return sum(1 for a in self.answers if a.status == AnswerStatus.SUCCESS)

    @property
    def average_confidence(self) -> float:
        if not self.answers:
            return 0.0
        return sum(a.confidence for a in self.answers) / len(self.answers)

    def get_answer(self, question_id: str) -> Optional[GeneratedAnswer]:
        """Find answer by question_id."""
        for ans in self.answers:
            if ans.question_id == question_id:
                return ans
        return None

    def get_answer_by_number(self, question_number: str) -> Optional[GeneratedAnswer]:
        """Find answer by question_number."""
        for ans in self.answers:
            if ans.question_number == question_number:
                return ans
        return None


class PipelineResult(BaseModel):
    """Result of running the end-to-end worksheet parsing and answering pipeline."""
    original_file: Path
    completed_file: Path
    worksheet: ParsedWorksheet
    answers: WorksheetAnswers
    success: bool = True
    summary: Dict[str, Any] = Field(default_factory=dict)
