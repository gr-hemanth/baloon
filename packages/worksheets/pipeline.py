"""End-to-end Worksheet Pipeline orchestrating parsing, answering, and filling."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from packages.worksheets.answer_engine import AnswerEngineFactory, BaseAnswerEngine
from packages.worksheets.answer_models import PipelineResult, WorksheetAnswers
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.models import ParsedWorksheet
from packages.worksheets.parser import WorksheetParser

logger = logging.getLogger(__name__)


class WorksheetPipeline:
    """Coordinated pipeline for worksheet parsing, solution drafting, and document filling."""

    def __init__(
        self,
        parser: Optional[WorksheetParser] = None,
        answer_engine: Optional[BaseAnswerEngine] = None,
        filler: Optional[DocxWorksheetFiller] = None,
    ):
        self.parser = parser or WorksheetParser()
        self.answer_engine = answer_engine or AnswerEngineFactory.get_engine()
        self.filler = filler or DocxWorksheetFiller()

    async def process(
        self,
        worksheet_path: Path,
        output_dir: Optional[Path] = None,
        context: Optional[Dict[str, Any]] = None,
        output_filename: Optional[str] = None,
    ) -> PipelineResult:
        """Execute complete pipeline: parse -> answer -> fill -> return result.
        
        Args:
            worksheet_path: Path to the original worksheet document.
            output_dir: Target directory for the completed document.
            context: Additional context (e.g. course metadata, student info).
            output_filename: Optional custom completed filename.
            
        Returns:
            PipelineResult containing paths, parsed model, answers, and summary.
        """
        path = Path(worksheet_path)
        if not path.exists():
            raise FileNotFoundError(f"Worksheet not found: {path}")

        logger.info("Starting worksheet pipeline for %s", path.name)

        # 1. Parse Worksheet
        parsed_worksheet: ParsedWorksheet = self.parser.parse(path)
        logger.info(
            "Parsed %s: %d questions identified across %d sections",
            path.name,
            parsed_worksheet.question_count,
            len(parsed_worksheet.sections),
        )

        # 2. Generate Answers
        answers: WorksheetAnswers = await self.answer_engine.generate_answers(
            parsed_worksheet, context
        )
        logger.info(
            "Generated %d answers (Avg confidence: %.2f)",
            answers.total_count,
            answers.average_confidence,
        )

        # 3. Fill Document Copy
        completed_file = self.filler.fill(
            original_file_path=path,
            worksheet=parsed_worksheet,
            answers=answers,
            output_dir=output_dir,
            output_filename=output_filename,
        )

        # 4. Summary & Verification
        summary = {
            "original_file": str(path),
            "completed_file": str(completed_file),
            "completed_file_size_bytes": completed_file.stat().st_size,
            "total_questions": parsed_worksheet.question_count,
            "answers_generated": answers.total_count,
            "success_count": answers.success_count,
            "average_confidence": round(answers.average_confidence, 3),
            "provider": answers.provider,
        }

        return PipelineResult(
            original_file=path,
            completed_file=completed_file,
            worksheet=parsed_worksheet,
            answers=answers,
            success=answers.success_count > 0 or parsed_worksheet.question_count == 0,
            summary=summary,
        )
