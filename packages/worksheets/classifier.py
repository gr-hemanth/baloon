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
from typing import Any, Dict, List, Optional, Tuple

from packages.worksheets.models import (
    ParsedQuestion,
    QuestionOption,
    QuestionType,
    ResponseMode,
)


class CodeIntentDetector:
    """Semantic and syntax-based detector to identify code intent and programming language."""

    # Explicit implementation verbs that indicate writing code/implementation
    CODE_IMPL_VERBS = [
        re.compile(r"\b(?:write|implement|code|program|develop|design|construct|create|build)\b", re.IGNORECASE),
        re.compile(r"\b(?:override|overload|inherit|extend|instantiate|declare|modify|convert)\b", re.IGNORECASE),
        re.compile(r"\bdemonstrate\b", re.IGNORECASE),
        re.compile(r"\bcomplete\s+(?:the\s+)?(?:code|function|class|method)\b", re.IGNORECASE),
    ]

    # Explicit explanation / theory verbs
    EXPLANATION_VERBS = [
        re.compile(r"\b(?:explain|describe|discuss|elaborate|clarify|detail)\b", re.IGNORECASE),
        re.compile(r"\b(?:use\s+of|significance\s+of|purpose\s+of|role\s+of)\b", re.IGNORECASE),
    ]

    # Pure theory prompts (questions that begin with or focus exclusively on theory)
    PURE_THEORY_STARTERS = [
        re.compile(r"^(?:explain|what\s+is|what\s+are|define|state|list|compare|differentiate|distinguish|discuss|elaborate|write\s+a\s+short\s+note|briefly\s+explain|give\s+(?:two|three|any|\d+))\b", re.IGNORECASE),
    ]

    # Programming entities / constructs
    PROGRAMMING_CONSTRUCTS = [
        re.compile(r"\b(?:class\s+hierarchy|superclass|subclass|base\s+class|derived\s+class)\b", re.IGNORECASE),
        re.compile(r"\b(?:class|interface|abstract\s+class|constructor|method|function)\b", re.IGNORECASE),
        re.compile(r"\b(?:inheritance|polymorphism|encapsulation|overriding|overloading|abstraction)\b", re.IGNORECASE),
        re.compile(r"\b(?:super|this|extends|implements|try-catch|exception|pointer|struct)\b", re.IGNORECASE),
        re.compile(r"\b(?:linked\s+list|binary\s+tree|stack|queue|hashmap|arraylist|array)\b", re.IGNORECASE),
        re.compile(r"\b(?:sql\s+query|select\s+query|stored\s+procedure|schema|table)\b", re.IGNORECASE),
        re.compile(r"\b(?:threads?|multithreading|runnable|concurrency|synchronization|synchronized|deadlock|sleep|join|wait|notify)\b", re.IGNORECASE),
        re.compile(r"\b(?:stream|lambda|generics|collection|iterator|socket|event|listener|regex|scanner)\b", re.IGNORECASE),
    ]

    # Algorithm / Pseudocode / Output trace patterns
    ALGORITHM_PATTERNS = [
        re.compile(r"\b(?:write|develop|give|provide)\s+(?:an?\s+)?algorithm\b", re.IGNORECASE),
        re.compile(r"\balgorithm\s+(?:to|for)\b", re.IGNORECASE),
    ]

    PSEUDOCODE_PATTERNS = [
        re.compile(r"\bpseudo-?code\b", re.IGNORECASE),
    ]

    OUTPUT_TRACE_PATTERNS = [
        re.compile(r"\b(?:what\s+is\s+the\s+output|predict\s+the\s+output|find\s+the\s+output|give\s+the\s+output)\b", re.IGNORECASE),
        re.compile(r"\btrace\s+the\s+(?:execution|output|code|variable)\b", re.IGNORECASE),
        re.compile(r"\boutput\s+of\s+the\s+following\b", re.IGNORECASE),
    ]

    # Language patterns
    LANGUAGE_PATTERNS = [
        ("java", re.compile(r"\bjava\b", re.IGNORECASE)),
        ("python", re.compile(r"\bpython\b", re.IGNORECASE)),
        ("cpp", re.compile(r"\b(?:c\+\+|cpp)\b", re.IGNORECASE)),
        ("csharp", re.compile(r"\b(?:c#|csharp)\b", re.IGNORECASE)),
        ("c", re.compile(r"\b(?:c\s+program(?:ming)?|using\s+c\b|in\s+c\b|c\s+language)\b", re.IGNORECASE)),
        ("sql", re.compile(r"\b(?:sql|query|queries|relational\s+database)\b", re.IGNORECASE)),
        ("javascript", re.compile(r"\b(?:javascript|js)\b", re.IGNORECASE)),
        ("typescript", re.compile(r"\b(?:typescript|ts)\b", re.IGNORECASE)),
        ("html", re.compile(r"\bhtml\b", re.IGNORECASE)),
        ("css", re.compile(r"\bcss\b", re.IGNORECASE)),
    ]

    @classmethod
    def detect_language(
        cls,
        text: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Detect the programming language from question text or course/worksheet context."""
        ctx = context or {}
        # 1. Direct match in question text
        for lang_name, pattern in cls.LANGUAGE_PATTERNS:
            if pattern.search(text):
                return lang_name

        # 2. Context checks (course_code, course_name, metadata)
        course_code = str(ctx.get("course_code") or "").upper()
        course_name = str(ctx.get("title") or ctx.get("course_name") or "").lower()

        if "21CSC203P" in course_code or "CSC203" in course_code:
            return "java"
        if "java" in course_name:
            return "java"
        if "python" in course_name:
            return "python"
        if "c programming" in course_name or "problem solving using c" in course_name:
            return "c"
        if "sql" in course_name or "database" in course_name or "dbms" in course_name:
            return "sql"

        if ctx.get("language"):
            return str(ctx["language"]).lower()

        return None

    @classmethod
    def detect(
        cls,
        text: str,
        context: Optional[Dict[str, Any]] = None,
        question_type: Optional[QuestionType] = None,
        has_table_targets: bool = False,
        has_targets: bool = False,
    ) -> Tuple[ResponseMode, Optional[str]]:
        """Detect the response mode and target programming language for a question."""
        ctx = context or {}
        detected_lang = cls.detect_language(text, ctx)

        # If question has table cell targets or is TABLE_CELL type
        if has_table_targets or question_type == QuestionType.TABLE_CELL:
            return ResponseMode.TABLE_VALUE, detected_lang

        # MCQ / ONE_WORD are text-only responses
        if question_type in (QuestionType.MCQ, QuestionType.ONE_WORD, QuestionType.FILL_IN_BLANK, QuestionType.TICK_SELECT):
            return ResponseMode.TEXT, detected_lang

        clean_text = text.strip()

        # 1. Output Tracing
        if any(p.search(clean_text) for p in cls.OUTPUT_TRACE_PATTERNS):
            return ResponseMode.OUTPUT_TRACE, detected_lang

        # 2. Algorithm
        if any(p.search(clean_text) for p in cls.ALGORITHM_PATTERNS):
            return ResponseMode.ALGORITHM, detected_lang

        # 3. Pseudocode
        if any(p.search(clean_text) for p in cls.PSEUDOCODE_PATTERNS):
            return ResponseMode.PSEUDOCODE, detected_lang

        # 4. Check for class hierarchy arrow patterns (e.g. Vehicle → Car → ElectricCar)
        has_named_class_arrow = bool(re.search(
            r"\b[A-Z][A-Za-z0-9_]*\s*(?:→|->|-->)\s*[A-Z][A-Za-z0-9_]*",
            clean_text,
        ))
        has_generic_arrow = bool(re.search(r"\b[A-Za-z0-9_]+\s*(?:→|->|-->)\s*[A-Za-z0-9_]+", clean_text))
        has_class_concept = any(p.search(clean_text) for p in cls.PROGRAMMING_CONSTRUCTS)
        has_arrow_hierarchy = has_named_class_arrow or (has_generic_arrow and has_class_concept)

        # 5. Check for implementation verbs
        has_impl_verb = any(p.search(clean_text) for p in cls.CODE_IMPL_VERBS)
        has_explanation = any(p.search(clean_text) for p in cls.EXPLANATION_VERBS)

        # 6. Check for pure theory starters
        starts_pure_theory = any(p.search(clean_text) for p in cls.PURE_THEORY_STARTERS)

        # If question starts with pure theory ("Explain...", "What is...", "Define...", "Compare...")
        # Check if it ALSO contains an explicit implementation instruction:
        # e.g. "Explain method overriding and implement an example in Java."
        has_explicit_impl_clause = bool(re.search(
            r"\b(?:and\s+)?(?:implement|write\s+(?:a\s+)?(?:program|code|class)|create\s+a\s+class|demonstrate\s+using\s+code)\b",
            clean_text,
            re.IGNORECASE,
        ))

        # Check course default language if programming constructs are present
        course_code = str(ctx.get("course_code") or "").upper()
        if (not detected_lang) and ("21CSC203P" in course_code or "CSC203" in course_code):
            detected_lang = "java"

        if starts_pure_theory and not has_explicit_impl_clause:
            # Pure theory question (e.g. "Explain inheritance in Java", "What is polymorphism?", "Compare abstract class and interface")
            return ResponseMode.TEXT, detected_lang

        if has_arrow_hierarchy or (has_impl_verb and has_class_concept) or has_explicit_impl_clause:
            # Explicit code implementation requested!
            if has_explanation:
                return ResponseMode.CODE_AND_EXPLANATION, detected_lang or "java"
            return ResponseMode.CODE, detected_lang or "java"

        # If question type was classified as CODE
        if question_type == QuestionType.CODE:
            if has_explanation:
                return ResponseMode.CODE_AND_EXPLANATION, detected_lang or "java"
            return ResponseMode.CODE, detected_lang or "java"

        return ResponseMode.TEXT, detected_lang


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
        re.compile(r"\bimplement\s+(?:a\s+)?(?:class|interface|method|function|threads?|program|algorithm)\b", re.IGNORECASE),
        re.compile(r"\bcreate\s+(?:two\s+|a\s+|multiple\s+)?(?:threads?)\b", re.IGNORECASE),
        re.compile(r"\bdemonstrate\s+(?:.*?\b)?(?:threads?|sleep|join)\b", re.IGNORECASE),
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
    def classify(
        cls,
        question: ParsedQuestion,
        context: Optional[Dict[str, Any]] = None,
    ) -> QuestionType:
        """Classify a ParsedQuestion domain object based on its attributes."""
        # Check if marks are already on the question or extractable from text
        marks = question.marks
        if marks is None:
            marks = cls.extract_marks(question.question_text)
            if marks is not None:
                question.marks = marks

        qtype = cls.classify_raw(
            text=question.question_text,
            options=question.options,
            marks=marks,
            section=question.section,
            context=question.context_or_activity,
        )

        ctx = dict(context or {})
        if question.formatting_metadata:
            ctx.update(question.formatting_metadata)

        has_table_targets = bool(
            question.targets and any(
                str(getattr(t, "target_type", "")).upper().startswith("TABLE")
                for t in question.targets
            )
        )
        resp_mode, lang = CodeIntentDetector.detect(
            text=question.question_text,
            context=ctx,
            question_type=qtype,
            has_table_targets=has_table_targets,
            has_targets=bool(question.targets),
        )
        question.response_mode = resp_mode
        if lang:
            question.language = lang

        # Determine available_space
        if question.targets:
            first_t = question.targets[0]
            if first_t.target_type == "table_cell" or (first_t.expected_length in ("word", "phrase", "short")):
                question.available_space = "compact"
            else:
                question.available_space = "complete"
        elif qtype in (QuestionType.ONE_WORD, QuestionType.FILL_IN_BLANK):
            question.available_space = "compact"
        else:
            question.available_space = "complete"

        question.question_type = qtype
        return qtype

    @classmethod
    def detect_code_intent(
        cls,
        text: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[ResponseMode, Optional[str]]:
        """Convenience method delegating to CodeIntentDetector."""
        return CodeIntentDetector.detect(text, context=context)

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
