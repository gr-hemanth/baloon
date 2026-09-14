"""Abstract interface and default implementation for document processing."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

from packages.worksheets.parser import WorksheetParser
from packages.worksheets.models import ParsedWorksheet


class WorksheetProcessor(ABC):
    """Abstract interface for document processing and worksheet answering.
    
    NOTE: Worksheet answering logic is explicitly deferred to later milestones.
    """

    @abstractmethod
    def parse_worksheet(self, document_path: Path) -> Dict[str, Any]:
        """Parse questions, sections, and metadata from downloaded worksheet file."""
        raise NotImplementedError("Worksheet parsing not implemented yet")

    @abstractmethod
    def process_solutions(self, parsed_data: Dict[str, Any]) -> Path:
        """Process answers and generate completed worksheet document."""
        raise NotImplementedError("Worksheet answering logic not implemented yet")


class DefaultWorksheetProcessor(WorksheetProcessor):
    """Default worksheet processor using generic parser engine."""

    def __init__(self, parser: Optional[WorksheetParser] = None):
        self.parser = parser or WorksheetParser()

    def parse_worksheet(self, document_path: Path) -> Dict[str, Any]:
        """Parse questions, sections, and metadata from downloaded worksheet file."""
        parsed: ParsedWorksheet = self.parser.parse(document_path)
        return parsed.to_dict()

    def process_solutions(self, parsed_data: Dict[str, Any]) -> Path:
        """Process answers and generate completed worksheet document.
        
        Deferred to Milestone 5 (AI Solution Engine).
        """
        raise NotImplementedError(
            "Solution generation is deferred to the AI solution engine milestone."
        )
