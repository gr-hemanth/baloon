"""Base abstraction for worksheet document parsers."""

from abc import ABC, abstractmethod
from pathlib import Path
from packages.worksheets.models import ParsedWorksheet


class BaseWorksheetParser(ABC):
    """Abstract base class for format-specific worksheet parsers (DOCX, PDF)."""

    @abstractmethod
    def parse(self, file_path: Path) -> ParsedWorksheet:
        """Parse a worksheet document file into a structured ParsedWorksheet model.
        
        Args:
            file_path: Path to the target document.
            
        Returns:
            ParsedWorksheet containing document metadata, sections, and questions.
            
        Raises:
            FileNotFoundError: If the target file does not exist.
            ValueError: If the document is malformed or cannot be parsed.
        """
        pass
