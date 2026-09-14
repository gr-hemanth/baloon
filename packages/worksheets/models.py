"""Domain models for worksheet parsing, questions, and document structure.

Defines structured domain entities for parsed worksheets, questions, options,
sections, and classification types across different document formats (DOCX, PDF).
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    """Classification of worksheet questions."""
    MCQ = "MCQ"
    ONE_WORD = "ONE_WORD"
    SHORT_ANSWER = "SHORT_ANSWER"
    LONG_ANSWER = "LONG_ANSWER"
    UNKNOWN = "UNKNOWN"


class QuestionOption(BaseModel):
    """An option choice for multiple choice questions."""
    key: str  # e.g., "A", "B", "C", "D" or "a", "b", "1", "2"
    text: str

    def __str__(self) -> str:
        return f"{self.key}. {self.text}"


class ParsedQuestion(BaseModel):
    """Structured question extracted from a worksheet document."""
    question_id: str
    question_number: Optional[str] = None  # e.g. "1", "1.a", "Q2", "Part A - 1"
    question_text: str
    question_type: QuestionType = QuestionType.UNKNOWN
    options: List[QuestionOption] = Field(default_factory=list)
    marks: Optional[float] = None
    section: Optional[str] = None
    source_order: int = 1
    source_location: Dict[str, Any] = Field(default_factory=dict)
    formatting_metadata: Dict[str, Any] = Field(default_factory=dict)
    sub_parts: List["ParsedQuestion"] = Field(default_factory=list)
    context_or_activity: Optional[str] = None


class WorksheetSection(BaseModel):
    """Section or heading boundary within a worksheet."""
    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    start_order: int = 1


class ParsedWorksheet(BaseModel):
    """Top-level structured representation of a parsed worksheet document."""
    filename: str
    file_format: str  # "docx" or "pdf"
    title: Optional[str] = None
    course_code: Optional[str] = None
    unit: Optional[int] = None
    session: Optional[int] = None
    slo: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    sections: List[WorksheetSection] = Field(default_factory=list)
    questions: List[ParsedQuestion] = Field(default_factory=list)
    raw_document_info: Dict[str, Any] = Field(default_factory=dict)

    def get_questions_by_type(self, qtype: QuestionType) -> List[ParsedQuestion]:
        """Filter parsed questions by their classified question type."""
        return [q for q in self.questions if q.question_type == qtype]

    @property
    def question_count(self) -> int:
        """Total number of questions extracted."""
        return len(self.questions)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return self.model_dump()


# Enable recursive model references for nested sub_parts
ParsedQuestion.model_rebuild()
