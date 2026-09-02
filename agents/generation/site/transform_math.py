#!/usr/bin/env python3
"""Transform blueprint markdown for Zola + MathJax rendering.

Zola's markdown engine (CommonMark) processes content before MathJax sees it.
This causes three classes of breakage:

1. \\( and \\[ are treated as escape sequences → delimiters vanish.
   Fix: convert to $...$ and $$...$$ which are not CommonMark escapes.

2. Inside math, \\! \\, \\; \\: \\{ \\} etc. are CommonMark escapes —
   the backslash is stripped.
   Fix: double the backslash (\\\\!) so CommonMark produces \\! for MathJax.

3. Underscores in math (_) can be misparsed as emphasis.
   Fix: add a space after _ so it is not a left-flanking delimiter.
"""

import re
import sys

LABEL_RE = re.compile(r"^(lem|thm|def|prop|cor|rem|ex)\w*:[\w-]+$")

# ASCII punctuation that CommonMark treats as escapable after a backslash.
_CM_PUNCT = set(r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~""")
_CM_PUNCT_RE = re.compile(
    r"\\(?=[" + re.escape(r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~""") + r"])"
)


def _is_label(s: str) -> bool:
    return bool(LABEL_RE.match(s.strip()))


def _protect_backslashes(math: str) -> str:
    """Double every \\ that precedes ASCII punctuation so CommonMark keeps it."""
    return _CM_PUNCT_RE.sub(r"\\\\", math)


def _fix_underscore(math: str) -> str:
    return re.sub(r"_(?=\S)", "_ ", math)


def _process_math(math: str) -> str:
    """Apply all math-content fixes."""
    math = _protect_backslashes(math)
    math = _fix_underscore(math)
    return math


def _convert_delimiters(text: str) -> str:
    """Normalise all math delimiters to $ / $$ and process content."""
    # Display math: \[...\] → $$...$$ (may span lines)
    text = re.sub(
        r"\\\[(.*?)\\\]",
        lambda m: "$$" + _process_math(m.group(1)) + "$$",
        text,
        flags=re.DOTALL,
    )
    # Inline math: \(...\) → $...$
    text = re.sub(
        r"\\\((.*?)\\\)",
        lambda m: "$" + _process_math(m.group(1)) + "$",
        text,
        flags=re.DOTALL,
    )
    # Backtick math → $...$ (label references stay as code)
    def _backtick_repl(m: re.Match) -> str:
        inner = m.group(1)
        if _is_label(inner):
            return m.group(0)
        return "$" + _process_math(inner) + "$"

    text = re.sub(r"(?<!`)`([^`]+)`(?!`)", _backtick_repl, text)
    return text


def _fix_existing_dollar_math(text: str) -> str:
    """Process math content inside pre-existing $...$ and $$...$$ regions."""
    # Display math $$...$$ first
    text = re.sub(
        r"(\$\$)(.*?)(\$\$)",
        lambda m: m.group(1) + _process_math(m.group(2)) + m.group(3),
        text,
        flags=re.DOTALL,
    )
    # Inline math $...$
    text = re.sub(
        r"(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)",
        lambda m: "$" + _process_math(m.group(1)) + "$",
        text,
    )
    return text


def transform(text: str) -> str:
    # Order matters: process pre-existing $/$$ first, then convert other
    # delimiters.  This ensures each math region is processed exactly once.
    text = _fix_existing_dollar_math(text)
    text = _convert_delimiters(text)
    return text


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} INPUT OUTPUT", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[1]) as f:
        content = f.read()
    with open(sys.argv[2], "w") as f:
        f.write(transform(content))
