"""Abstract interface and default implementation for document processing."""

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

from packages.worksheets.answer_engine import BaseAnswerEngine
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.models import ParsedWorksheet
from packages.worksheets.parser import WorksheetParser
from packages.worksheets.pipeline import WorksheetPipeline


class WorksheetProcessor(ABC):
    """Abstract interface for document processing and worksheet answering."""

    @abstractmethod
    def parse_worksheet(self, document_path: Path) -> Dict[str, Any]:
        """Parse questions, sections, and metadata from downloaded worksheet file."""
        raise NotImplementedError("Worksheet parsing not implemented yet")

    @abstractmethod
    def process_solutions(self, parsed_data: Dict[str, Any], document_path: Optional[Path] = None) -> Path:
        """Process answers and generate completed worksheet document."""
        raise NotImplementedError("Worksheet answering logic not implemented yet")


class DefaultWorksheetProcessor(WorksheetProcessor):
    """Default worksheet processor using generic parser, answering engine, and filler."""

    def __init__(
        self,
        parser: Optional[WorksheetParser] = None,
        answer_engine: Optional[BaseAnswerEngine] = None,
        filler: Optional[DocxWorksheetFiller] = None,
    ):
        self.parser = parser or WorksheetParser()
        self.pipeline = WorksheetPipeline(
            parser=self.parser,
            answer_engine=answer_engine,
            filler=filler,
        )

    def parse_worksheet(self, document_path: Path) -> Dict[str, Any]:
        """Parse questions, sections, and metadata from downloaded worksheet file."""
        parsed: ParsedWorksheet = self.parser.parse(document_path)
        data = parsed.to_dict()
        data["document_path"] = str(document_path)
        return data

    def process_solutions(
        self,
        parsed_data: Dict[str, Any],
        document_path: Optional[Path] = None,
    ) -> Path:
        """Generate answers and create completed worksheet copy."""
        doc_path_str = document_path or parsed_data.get("document_path")
        if not doc_path_str:
            raise ValueError(
                "Cannot process solutions without a target document_path. "
                "Provide document_path in parsed_data or as an argument."
            )
        target_path = Path(doc_path_str)

        # Run pipeline asynchronously (or in current loop)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # In an active loop, we run via task or create new loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    self.pipeline.process(target_path, context=parsed_data)
                ).result()
        else:
            result = asyncio.run(self.pipeline.process(target_path, context=parsed_data))

        return result.completed_file
