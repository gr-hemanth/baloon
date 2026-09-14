"""Unified Worksheet Parser dispatching to format-specific engines."""

from pathlib import Path
from typing import Optional

from packages.worksheets.base_parser import BaseWorksheetParser
from packages.worksheets.docx_parser import DocxWorksheetParser
from packages.worksheets.pdf_parser import PdfWorksheetParser
from packages.worksheets.models import ParsedWorksheet


class WorksheetParser(BaseWorksheetParser):
    """Generic worksheet parser supporting DOCX and PDF documents."""

    def __init__(
        self,
        docx_parser: Optional[DocxWorksheetParser] = None,
        pdf_parser: Optional[PdfWorksheetParser] = None,
    ):
        self.docx_parser = docx_parser or DocxWorksheetParser()
        self.pdf_parser = pdf_parser or PdfWorksheetParser()

    def parse(self, file_path: Path) -> ParsedWorksheet:
        """Parse worksheet from file path, automatically selecting format engine.
        
        Args:
            file_path: Path to the .docx or .pdf worksheet document.
            
        Returns:
            ParsedWorksheet structured model.
            
        Raises:
            FileNotFoundError: If target file does not exist.
            ValueError: If file format is unsupported.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Worksheet file not found: {path}")

        suffix = path.suffix.lower()
        if suffix == ".docx":
            return self.docx_parser.parse(path)
        elif suffix == ".pdf":
            return self.pdf_parser.parse(path)
        else:
            raise ValueError(
                f"Unsupported worksheet format '{suffix}'. Only .docx and .pdf are supported."
            )
