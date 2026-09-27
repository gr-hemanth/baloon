"""End-to-end Worksheet Pipeline orchestrating parsing, answering, and filling."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from packages.worksheets.answer_engine import AnswerEngineFactory, BaseAnswerEngine
from packages.worksheets.answer_models import AnswerStatus, PipelineResult, WorksheetAnswers
from packages.worksheets.code_validator import CodeAnswerValidator
from packages.worksheets.exceptions import AnswerGenerationError
from packages.worksheets.filler import DocxWorksheetFiller
from packages.worksheets.models import ParsedWorksheet, QuestionType, ResponseMode
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

        # 3. Post-generation Code Answer Validation (CRITICAL - DO NOT ALLOW EMPTY/INVALID CODE TO FILLER)
        for q in parsed_worksheet.questions:
            is_code_q = (
                getattr(q, "response_mode", None) in (ResponseMode.CODE, ResponseMode.CODE_AND_EXPLANATION)
                or q.question_type == QuestionType.CODE
            )
            if is_code_q:
                q_ans = answers.get_answer(q.question_id)
                if not q_ans or not q_ans.answer_text or q_ans.status == AnswerStatus.ERROR or q_ans.answer_text.startswith("[Empty"):
                    raise AnswerGenerationError(
                        f"Worksheet filler blocked: Question '{q.question_id}' ('{q.question_text[:60]}') "
                        "has no valid code answer."
                    )
                val = CodeAnswerValidator.validate(q_ans.answer_text, q, q_ans.language or q.language)
                if not val.is_valid:
                    raise AnswerGenerationError(
                        f"Worksheet filler blocked: Question '{q.question_id}' failed post-generation code validation: {val.reason}"
                    )
                # Ensure cleaned code is stored
                q_ans.answer_text = val.cleaned_code

        # 4. Fill Document Copy
        if context and "student_info" in context:
            self.filler.header_filler = StudentHeaderFiller(context["student_info"])

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
            "verification": (
                self.filler.last_verification.model_dump()
                if self.filler.last_verification
                else None
            ),
            "verification_summary": (
                self.filler.last_verification.to_summary_str()
                if self.filler.last_verification
                else None
            ),
        }

        is_valid = True
        if self.filler.last_verification:
            is_valid = self.filler.last_verification.is_valid

        return PipelineResult(
            original_file=path,
            completed_file=completed_file,
            worksheet=parsed_worksheet,
            answers=answers,
            success=(answers.success_count > 0 or parsed_worksheet.question_count == 0) and is_valid,
            summary=summary,
        )
