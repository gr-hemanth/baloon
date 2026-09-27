"""Post-generation validator for programming code answers.

Validates that code answers:
1. Are not empty (or placeholder text).
2. Are not merely prose or theoretical instructions.
3. Contain expected programming language constructs.
4. Represent required programming constructs from the question intent.
5. Have plausible syntax (balanced braces/parens, statements, etc.).
6. Contain no raw Markdown code fences (```).
7. Contain no AI filler ("Here is the code...", "Certainly!").
8. Are not theory-only explanations.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from packages.worksheets.models import ParsedQuestion, ResponseMode


@dataclass
class CodeValidationResult:
    """Result of code answer validation."""
    is_valid: bool
    reason: Optional[str] = None
    cleaned_code: Optional[str] = None
    missing_constructs: Optional[List[str]] = None


class CodeAnswerValidator:
    """Semantic and syntax validator for code and code-and-explanation answers."""

    # Disallowed placeholder phrases indicating missing / errored generation
    PLACEHOLDER_PHRASES = [
        "[empty answer returned by ai]",
        "[question omitted from ai response - manual review required]",
        "[answer pending manual review]",
        "[no answer provided]",
        "todo:",
        "// to be implemented",
    ]

    # Conversational AI filler to clean from responses
    FILLER_PREFIXES = [
        re.compile(r"^(?:Certainly!|Sure!|Here is the (?:Java |Python |C |C\+\+ )?(?:code|program|implementation|solution)[^:\n]*:\s*|Below is the [^:\n]*:\s*)\n*", re.IGNORECASE),
        re.compile(r"^(?:As requested,?[^\n]*\n+|In this (?:program|code),?[^\n]*\n+)", re.IGNORECASE),
    ]

    @classmethod
    def clean_code_text(cls, raw_text: str) -> str:
        """Strip markdown fences, leading/trailing whitespace, and AI preambles."""
        if not raw_text:
            return ""
        text = raw_text.strip()

        # 1. Strip conversational filler prefix if present
        for pattern in cls.FILLER_PREFIXES:
            text = pattern.sub("", text).strip()

        # 2. Strip Markdown code fences if present (e.g. ```java ... ```)
        text = re.sub(r"^```[a-zA-Z0-9_\-\+\#]*\s*\n?", "", text, flags=re.MULTILINE)
        text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)

        # 3. Strip redundant leading "Answer:" or "Solution:" label
        text = re.sub(r"^(?:Answer|Ans|Solution)\s*[:\-–—]\s*", "", text, flags=re.IGNORECASE).strip()

        return text

    @classmethod
    def extract_code_and_explanation(cls, text: str) -> Tuple[str, str]:
        """Split a CODE_AND_EXPLANATION answer into code portion and explanation portion."""
        cleaned = cls.clean_code_text(text)
        # Check for explicit "Explanation:" marker
        match = re.search(r"\n\s*(?:Explanation|Brief Explanation|Description|Notes?)\s*[:\-–—]\s*\n?", cleaned, re.IGNORECASE)
        if match:
            code_part = cleaned[:match.start()].strip()
            expl_part = cleaned[match.end():].strip()
            return code_part, expl_part

        # Check for last paragraph being prose explanation
        lines = [line.rstrip() for line in cleaned.splitlines()]
        # If code ends with closing brace '}' and subsequent lines are text
        last_brace_idx = -1
        for idx, line in enumerate(lines):
            if "}" in line:
                last_brace_idx = idx

        if last_brace_idx != -1 and last_brace_idx + 1 < len(lines):
            code_part = "\n".join(lines[:last_brace_idx + 1]).strip()
            expl_part = "\n".join(lines[last_brace_idx + 1:]).strip()
            if expl_part:
                return code_part, expl_part

        return cleaned, ""

    @classmethod
    def validate(
        cls,
        answer_text: Optional[str],
        question: ParsedQuestion,
        language: Optional[str] = None,
    ) -> CodeValidationResult:
        """Comprehensive validation of a generated answer for code questions.
        
        Args:
            answer_text: The raw answer text returned by the model.
            question: The ParsedQuestion being answered.
            language: Programming language (defaulting to question.language or "java").
            
        Returns:
            CodeValidationResult with validation status, failure reason, and cleaned code.
        """
        # Rule 1: Answer is not empty
        if not answer_text or not str(answer_text).strip():
            return CodeValidationResult(is_valid=False, reason="Answer is empty.")

        cleaned = cls.clean_code_text(str(answer_text))
        if not cleaned:
            return CodeValidationResult(is_valid=False, reason="Answer contains only whitespace or markdown fences.")

        cleaned_lower = cleaned.lower()
        for placeholder in cls.PLACEHOLDER_PHRASES:
            if placeholder in cleaned_lower:
                return CodeValidationResult(is_valid=False, reason=f"Answer contains placeholder text: '{placeholder}'.")

        # Check Markdown fences
        if "```" in cleaned:
            return CodeValidationResult(is_valid=False, reason="Answer contains unstripped Markdown code fences (```).")

        # Determine target mode and language
        resp_mode = getattr(question, "response_mode", ResponseMode.CODE)
        detected_l = None
        if hasattr(question, "question_text"):
            from packages.worksheets.classifier import CodeIntentDetector
            detected_l = CodeIntentDetector.detect_language(question.question_text)
        target_lang = (language or getattr(question, "language", None) or detected_l or "java").lower()

        # Handle CODE_AND_EXPLANATION vs CODE
        code_part = cleaned
        expl_part = ""
        if resp_mode == ResponseMode.CODE_AND_EXPLANATION:
            code_part, expl_part = cls.extract_code_and_explanation(cleaned)

        # Rule 2 & 8: Answer is not merely prose or theory-only
        prose_check = cls._check_is_merely_prose(code_part, target_lang)
        if prose_check:
            return CodeValidationResult(is_valid=False, reason=prose_check)

        # Rule 3: Expected language constructs are present
        lang_check = cls._check_language_constructs(code_part, target_lang)
        if lang_check:
            return CodeValidationResult(is_valid=False, reason=lang_check)

        # Rule 4: Required programming constructs from question are represented
        missing_constructs = cls._check_required_constructs(code_part, question, target_lang)
        if missing_constructs:
            return CodeValidationResult(
                is_valid=False,
                reason=f"Code is missing required programming constructs from question: {', '.join(missing_constructs)}.",
                missing_constructs=missing_constructs,
            )

        # Rule 5: Plausible syntax (balanced braces, parens, statements)
        syntax_check = cls._check_plausible_syntax(code_part, target_lang)
        if syntax_check:
            return CodeValidationResult(is_valid=False, reason=syntax_check)

        # For CODE_AND_EXPLANATION, ensure explanation exists and is not empty
        if resp_mode == ResponseMode.CODE_AND_EXPLANATION:
            if not expl_part or len(expl_part.strip()) < 10:
                return CodeValidationResult(
                    is_valid=False,
                    reason="CODE_AND_EXPLANATION requires both actual code and an explanation section, but explanation is missing.",
                )

        return CodeValidationResult(is_valid=True, cleaned_code=cleaned)

    @classmethod
    def _check_is_merely_prose(cls, text: str, language: str) -> Optional[str]:
        """Detect if text is descriptive English prose without actual code implementation."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "Answer contains no code lines."

        # Punctuation and code token counters
        has_braces = "{" in text or "}" in text
        has_semicolon = ";" in text
        has_equals = "=" in text
        has_parens = "(" in text and ")" in text

        # Check for typical conversational/instructional prose without code structure
        prose_starters = [
            "create two runnable",
            "create two threads",
            "to implement",
            "to demonstrate",
            "you can implement",
            "first, create",
            "we can create",
            "in java,",
            "this program",
            "start both threads to run",
            "one prints even numbers",
        ]
        text_lower = text.lower()
        if any(text_lower.startswith(p) for p in prose_starters) and not has_braces and not has_semicolon:
            return "Answer is instructional prose rather than executable code."

        if language == "java":
            # Real Java code MUST contain at least one class definition, interface, or method, and semicolons/braces
            has_java_decl = bool(re.search(r"\b(?:class|interface|enum|record|void|public|private|protected)\b", text))
            if not has_java_decl and not has_braces and not has_semicolon:
                return "Answer contains no Java declarations (class, interface, method) or statements."

        elif language == "python":
            has_py_decl = bool(re.search(r"\b(?:def|class|import|from)\b", text))
            has_py_colon = ":" in text
            if not has_py_decl and not has_py_colon:
                return "Answer contains no Python declarations (def, class) or statements."

        elif language in ("c", "cpp"):
            has_c_decl = bool(re.search(r"\b(?:#include|int\s+main|void|struct|class)\b", text))
            if not has_c_decl and not has_semicolon:
                return "Answer contains no C/C++ declarations or statements."

        # Word count vs code token ratio check
        words = re.findall(r"\b[A-Za-z]+\b", text)
        if len(words) > 10 and not has_braces and not has_semicolon and not has_parens:
            return "Answer consists purely of descriptive English words without programming syntax."

        return None

    @classmethod
    def _check_language_constructs(cls, text: str, language: str) -> Optional[str]:
        """Ensure expected programming language constructs are present."""
        if language == "java":
            # Java signals: class/interface/method/types
            has_class = bool(re.search(r"\b(?:class|interface|enum|record)\s+[A-Za-z0-9_]+", text))
            has_types = bool(re.search(r"\b(?:void|int|double|float|boolean|String|Thread|Runnable)\b", text))
            has_braces = "{" in text and "}" in text
            if not (has_class or (has_types and has_braces)):
                return "Expected Java code constructs (class, method, typed declarations) not found."

        elif language == "python":
            has_py = bool(re.search(r"\b(?:def\s+[A-Za-z0-9_]+|class\s+[A-Za-z0-9_]+|import\s+[A-Za-z0-9_]+)\b", text))
            if not has_py and ":" not in text:
                return "Expected Python code constructs (def, class, :) not found."

        elif language in ("c", "cpp"):
            has_c = bool(re.search(r"\b(?:#include|int\s+main|void\s+[A-Za-z0-9_]+)\b", text))
            if not has_c:
                return "Expected C/C++ code constructs not found."

        elif language == "sql":
            has_sql = bool(re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE\s+TABLE)\b", text, re.IGNORECASE))
            if not has_sql:
                return "Expected SQL query constructs (SELECT, INSERT, etc.) not found."

        return None

    @classmethod
    def _check_required_constructs(
        cls,
        code: str,
        question: ParsedQuestion,
        language: str,
    ) -> List[str]:
        """Verify that constructs requested in the question are represented in the code."""
        missing = []
        q_text = question.question_text.lower()
        code_lower = code.lower()

        # 1. Thread requirements
        if "thread" in q_text or "threads" in q_text:
            has_thread = (
                "thread" in code_lower
                or "runnable" in code_lower
                or "threading" in code_lower
                or ".start()" in code_lower
                or "run()" in code_lower
            )
            if not has_thread:
                missing.append("Thread or Runnable construct")

        # 2. Two threads even and odd
        if ("two thread" in q_text or "two threads" in q_text or ("even" in q_text and "odd" in q_text)):
            # Must represent even and odd logic
            has_even = bool(re.search(r"\b(?:even|%\s*2\s*==\s*0|\+=\s*2)\b", code, re.IGNORECASE))
            has_odd = bool(re.search(r"\b(?:odd|%\s*2\s*!=\s*0|%\s*2\s*==\s*1)\b", code, re.IGNORECASE))
            if not has_even:
                missing.append("even number logic")
            if not has_odd:
                missing.append("odd number logic")

            # Must have two thread definitions or two thread starts
            start_count = len(re.findall(r"\.start\s*\(\s*\)", code))
            thread_classes = len(re.findall(r"\bclass\s+\w+\s+(?:extends\s+Thread|implements\s+Runnable)\b", code))
            if start_count < 2 and thread_classes < 2 and "Thread(" not in code:
                missing.append("two thread instances or definitions")

        # 3. Sleep and Join requirements
        if "sleep" in q_text:
            if not re.search(r"\b(?:Thread\.sleep|\.sleep)\s*\(", code):
                missing.append("Thread.sleep(...) call")
        if "join" in q_text:
            if not re.search(r"\b\.join\s*\(", code):
                missing.append("join() call")

        # 4. Interface implementation requirements
        if "interface" in q_text and ("implement" in q_text or "class" in q_text):
            if "interface " not in code and "interface\t" not in code:
                missing.append("interface definition")
            if "implements " not in code and "implements\t" not in code:
                missing.append("implements clause")

        # 5. Inheritance / Subclass requirements
        if "superclass" in q_text or "subclass" in q_text or "inheritance" in q_text or "extends" in q_text or "class hierarchy" in q_text:
            if language == "java":
                if "extends " not in code and "implements " not in code:
                    missing.append("inheritance (extends clause)")

        # 6. Method overriding requirements
        if "override" in q_text or "overriding" in q_text:
            if language == "java":
                if "@override" not in code_lower and "super." not in code_lower:
                    missing.append("@Override annotation or super call")

        # 7. Specific literal string requirements (e.g. print "Hello")
        if '"hello"' in q_text or "'hello'" in q_text or "hello using a thread" in q_text:
            if "hello" not in code_lower:
                missing.append("'Hello' output string")

        return missing

    @classmethod
    def _check_plausible_syntax(cls, code: str, language: str) -> Optional[str]:
        """Check basic structural and syntactic sanity (e.g. balanced braces)."""
        if language in ("java", "c", "cpp"):
            open_braces = code.count("{")
            close_braces = code.count("}")
            if open_braces == 0 and close_braces == 0:
                return "Code contains no block braces ({ })."
            if open_braces != close_braces:
                return f"Unbalanced braces in code: {open_braces} open '{{' vs {close_braces} close '}}'."

            open_parens = code.count("(")
            close_parens = code.count(")")
            if open_parens != close_parens:
                return f"Unbalanced parentheses in code: {open_parens} open '(' vs {close_parens} close ')'."

        elif language == "python":
            open_parens = code.count("(")
            close_parens = code.count(")")
            if open_parens != close_parens:
                return f"Unbalanced parentheses in Python code: {open_parens} open '(' vs {close_parens} close ')'."

        return None
