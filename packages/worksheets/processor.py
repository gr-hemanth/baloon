from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any


class WorksheetProcessor(ABC):
    """Abstract interface for document processing and worksheet answering.
    
    NOTE: Worksheet answering logic is explicitly deferred to later iterations.
    """

    @abstractmethod
    def parse_worksheet(self, document_path: Path) -> Dict[str, Any]:
        """Parse questions, sections, and metadata from downloaded worksheet file."""
        raise NotImplementedError("Worksheet parsing not implemented yet")

    @abstractmethod
    def process_solutions(self, parsed_data: Dict[str, Any]) -> Path:
        """Process answers and generate completed worksheet document."""
        raise NotImplementedError("Worksheet answering logic not implemented yet")
