"""Question classification engine using structural and contextual signals.

Determines whether an extracted item is:
- MCQ (Multiple Choice Questions)
- ONE_WORD (Fill in the blanks, one-word, True/False)
- SHORT_ANSWER (Conceptual, definitions, 1-4 marks)
- LONG_ANSWER (Descriptive, 5+ marks, essays, design thinking/simulations)
- UNKNOWN

Uses structural signals (options, blanks, marks, headings, question keywords)
without requiring external AI API calls.
"""

import re
from typing import List, Optional, Tuple

from packages.worksheets.models import ParsedQuestion, QuestionOption, QuestionType


class QuestionClassifier:
    """Classifies worksheet questions based on structural and syntactic signals."""

    # Regex patterns for marks extraction
    MARKS_PATTERNS = [
        re.compile(r"\[\s*(\d+(?:\.\d+)?)\s*(?:marks?|m)\s*\]", re.IGNORECASE),
        re.compile(r"\(\s*(\d+(?:\.\d+)?)\s*(?:marks?|m)\s*\)", re.IGNORECASE),
        re.compile(r"\[\s*(\d+(?:\.\d+)?)\s*\](?:\s*$|\s*\n)", re.IGNORECASE),
        re.compile(r"\((\d+(?:\.\d+)?)\)(?:\s*$|\s*\n)", re.IGNORECASE),
        re.compile(r"\b(\d+(?:\.\d+)?)\s+marks?\b", re.IGNORECASE),
    ]

    # Fill-in-the-blank / One-word patterns
    BLANK_PATTERNS = [
        re.compile(r"_{3,}"),                  # _____
        re.compile(r"\(\s*\)"),                # ( )
        re.compile(r"\[\s*\]"),                # [ ]
        re.compile(r"\(\s*\.{3,}\s*\)"),        # (...)
        re.compile(r"<blank>", re.IGNORECASE),
        re.compile(r"\{blank\}", re.IGNORECASE),
    ]

    ONE_WORD_KEYWORDS = [
        re.compile(r"\bfill\s+in\s+the\s+blanks?\b", re.IGNORECASE),
        re.compile(r"\bone\s+word\b", re.IGNORECASE),
        re.compile(r"\bstate\s+the\s+term\b", re.IGNORECASE),
        re.compile(r"\bwhat\s+is\s+the\s+term\b", re.IGNORECASE),
        re.compile(r"\btrue\s+or\s+false\b", re.IGNORECASE),
        re.compile(r"\b\(T\/F\)\b", re.IGNORECASE),
        re.compile(r"\b\[T\/F\]\b", re.IGNORECASE),
        re.compile(r"\bexpand\s+the\s+(?:acronym|abbreviation)\b", re.IGNORECASE),
        re.compile(r"\bname\s+the\s+(?:following|device|protocol|algorithm|type|layer|tool|pattern)\b", re.IGNORECASE),
    ]

    # Short answer keywords
    SHORT_ANSWER_KEYWORDS = [
        re.compile(r"\bdefine\b", re.IGNORECASE),
        re.compile(r"\bwhat\s+is\b", re.IGNORECASE),
        re.compile(r"\bwhat\s+are\b", re.IGNORECASE),
        re.compile(r"\bstate\b", re.IGNORECASE),
        re.compile(r"\blist\b", re.IGNORECASE),
        re.compile(r"\bgive\s+(?:two|three|any|\d+)\b", re.IGNORECASE),
        re.compile(r"\bdifferentiate\s+between\b", re.IGNORECASE),
        re.compile(r"\bdistinguish\s+between\b", re.IGNORECASE),
        re.compile(r"\bbriefly\s+explain\b", re.IGNORECASE),
        re.compile(r"\bwrite\s+a\s+short\s+note\b", re.IGNORECASE),
        re.compile(r"\bmention\b", re.IGNORECASE),
        re.compile(r"\boutline\b", re.IGNORECASE),
        re.compile(r"\bwhy\s+is\b", re.IGNORECASE),
        re.compile(r"\bhow\s+does\b", re.IGNORECASE),
    ]

    # Long answer keywords
    LONG_ANSWER_KEYWORDS = [
        re.compile(r"\bexplain\s+in\s+detail\b", re.IGNORECASE),
        re.compile(r"\bdiscuss\s+in\s+detail\b", re.IGNORECASE),
        re.compile(r"\belaborate\b", re.IGNORECASE),
        re.compile(r"\bdescribe\s+the\s+architecture\b", re.IGNORECASE),
        re.compile(r"\bdesign\s+and\s+(?:develop|implement)\b", re.IGNORECASE),
        re.compile(r"\bdraw\s+and\s+explain\b", re.IGNORECASE),
        re.compile(r"\bcase\s+study\b", re.IGNORECASE),
        re.compile(r"\brole-play\b", re.IGNORECASE),
        re.compile(r"\bsimulation\b", re.IGNORECASE),
        re.compile(r"\bdesign\s+thinking\b", re.IGNORECASE),
        re.compile(r"\bworkshop\b", re.IGNORECASE),
        re.compile(r"\bproject\s+proposal\b", re.IGNORECASE),
        re.compile(r"\bcritically\s+analyze\b", re.IGNORECASE),
        re.compile(r"\bderive\s+the\s+expression\b", re.IGNORECASE),
    ]

    # Code and technical keywords
    CODE_KEYWORDS = [
        re.compile(r"\bwrite\s+(?:a\s+)?(?:java|python|c\+\+|c#|c|sql|javascript|html|css)\b", re.IGNORECASE),
        re.compile(r"\bwrite\s+(?:a\s+)?(?:program|function|method|class|interface|query|script|code)\b", re.IGNORECASE),
        re.compile(r"\bimplement\s+(?:a\s+)?(?:class|interface|method|function)\b", re.IGNORECASE),
    ]

    # Pseudocode keywords
    PSEUDOCODE_KEYWORDS = [
        re.compile(r"\bwrite\s+(?:the\s+)?pseudo-?code\b", re.IGNORECASE),
        re.compile(r"\bpseudo-?code\b", re.IGNORECASE),
        re.compile(r"\bwrite\s+(?:an?\s+)?algorithm\b", re.IGNORECASE),
    ]

    # Output / tracing keywords
    OUTPUT_TRACING_KEYWORDS = [
        re.compile(r"\bwhat\s+is\s+the\s+output\b", re.IGNORECASE),
        re.compile(r"\bpredict\s+the\s+output\b", re.IGNORECASE),
        re.compile(r"\btrace\s+the\s+(?:execution|output|code)\b", re.IGNORECASE),
        re.compile(r"\bfind\s+the\s+output\b", re.IGNORECASE),
    ]

    # Tick / select keywords
    TICK_SELECT_KEYWORDS = [
        re.compile(r"\btick\s+(?:one|any|either)\b", re.IGNORECASE),
        re.compile(r"\bselect\s+(?:one|any)\s+and\s+explain\b", re.IGNORECASE),
        re.compile(r"\bchoose\s+one\s+and\s+explain\b", re.IGNORECASE),
    ]

    @classmethod
    def extract_marks(cls, text: str) -> Optional[float]:
        """Extract explicit marks from question text if present."""
        for pattern in cls.MARKS_PATTERNS:
            match = pattern.search(text)
            if match:
                try:
                    return float(match.group(1))
                except (ValueError, IndexError):
                    continue
        return None

    @classmethod
    def extract_inline_options(cls, text: str) -> Tuple[str, List[QuestionOption]]:
        """Extract options that are embedded within the question text itself.
        
        Example:
            'What is SDLC? A. Process B. Code C. Bug D. Test'
            Returns: ('What is SDLC?', [QuestionOption(key='A', text='Process'), ...])
        """
        # Look for sequences like (A) ... (B) ... or A. ... B. ...
        pattern = re.compile(
            r"(?:^|\s+)(?:\(?([A-Da-d])[\.\)]|\[([A-Da-d])\])\s+(.*?)(?=(?:\s+(?:\(?[A-Da-d][\.\)]|\[[A-Da-d]\])\s+)|$)",
            re.DOTALL
        )
        matches = list(pattern.finditer(text))
        if len(matches) >= 2:
            first_match_start = matches[0].start()
            clean_question = text[:first_match_start].strip()
            options: List[QuestionOption] = []
            for m in matches:
                key = (m.group(1) or m.group(2)).upper()
                opt_text = m.group(3).strip()
                options.append(QuestionOption(key=key, text=opt_text))
            return clean_question, options
        return text, []

    @classmethod
    def classify(cls, question: ParsedQuestion) -> QuestionType:
        """Classify a ParsedQuestion domain object based on its attributes."""
        # Check if marks are already on the question or extractable from text
        marks = question.marks
        if marks is None:
            marks = cls.extract_marks(question.question_text)
            if marks is not None:
                question.marks = marks

        return cls.classify_raw(
            text=question.question_text,
            options=question.options,
            marks=marks,
            section=question.section,
            context=question.context_or_activity,
        )

    @classmethod
    def classify_raw(
        cls,
        text: str,
        options: Optional[List[QuestionOption]] = None,
        marks: Optional[float] = None,
        section: Optional[str] = None,
        context: Optional[str] = None,
    ) -> QuestionType:
        """Perform classification using raw attributes and text features."""
        opts = options or []
        combined_text = f"{section or ''} {text} {context or ''}".strip()
        sec_lower = (section or "").lower()

        # 1. MCQ Detection
        # Signal A: Explicit options list (2 or more)
        if len(opts) >= 2:
            return QuestionType.MCQ

        # Signal B: Embedded options in text
        _, inline_opts = cls.extract_inline_options(text)
        if len(inline_opts) >= 2:
            return QuestionType.MCQ

        # Signal C: Section says Multiple Choice and has choice phrasing
        if any(kw in sec_lower for kw in ["multiple choice", "mcq", "choose the best", "choose the correct"]):
            if len(opts) >= 1 or any(re.search(rf"\b{opt}\b", text) for opt in ["option", "choice", "following"]):
                return QuestionType.MCQ

        # 2. ONE_WORD / Fill in the Blanks Detection
        # Signal A: Presence of blanks (___, ( ), [ ])
        if any(pattern.search(text) for pattern in cls.BLANK_PATTERNS):
            return QuestionType.ONE_WORD

        # Signal B: Keywords in text or section
        if any(kw.search(text) for kw in cls.ONE_WORD_KEYWORDS):
            return QuestionType.ONE_WORD

        if any(kw in sec_lower for kw in ["fill in", "blanks", "one word", "true or false", "true/false"]):
            return QuestionType.ONE_WORD

        # 3. TICK_SELECT Detection
        if any(kw.search(text) for kw in cls.TICK_SELECT_KEYWORDS):
            return QuestionType.TICK_SELECT

        # 4. CODE / PSEUDOCODE / OUTPUT_TRACING Detection
        if any(kw.search(text) for kw in cls.CODE_KEYWORDS):
            return QuestionType.CODE

        if any(kw.search(text) for kw in cls.PSEUDOCODE_KEYWORDS):
            return QuestionType.PSEUDOCODE

        if any(kw.search(text) for kw in cls.OUTPUT_TRACING_KEYWORDS):
            return QuestionType.OUTPUT_TRACING

        # 5. LONG_ANSWER Detection
        # Signal A: High marks (>= 5 marks)
        if marks is not None and marks >= 5.0:
            return QuestionType.LONG_ANSWER

        # Signal B: Long answer section names
        if any(kw in sec_lower for kw in ["part c", "part-c", "long answer", "essay", "descriptive", "case study", "scenario", "activity"]):
            return QuestionType.LONG_ANSWER

        # Signal C: Long answer keywords in question text or context
        if any(kw.search(combined_text) for kw in cls.LONG_ANSWER_KEYWORDS):
            # Unless explicitly marked with very small marks like 1 or 2
            if marks is None or marks >= 4.0:
                return QuestionType.LONG_ANSWER

        # 4. SHORT_ANSWER Detection
        # Signal A: Low marks (1 to 4 marks)
        if marks is not None and 1.0 <= marks <= 4.0:
            return QuestionType.SHORT_ANSWER

        # Signal B: Short answer section names
        if any(kw in sec_lower for kw in ["part b", "part-b", "short answer", "brief questions", "conceptual questions", "part a", "part-a"]):
            return QuestionType.SHORT_ANSWER

        # Signal C: Short answer keywords
        if any(kw.search(text) for kw in cls.SHORT_ANSWER_KEYWORDS):
            return QuestionType.SHORT_ANSWER

        # 5. Length-based Heuristic Fallback
        stripped = text.strip()
        if len(stripped) > 280:
            return QuestionType.LONG_ANSWER
        elif len(stripped) > 15:
            # Typical single sentence or question prompt
            return QuestionType.SHORT_ANSWER

        return QuestionType.UNKNOWN
