"""The SQL lexer and statement splitter shared by the components that read a step's SQL text.

Input: SQL text. Output: its statements, each a tuple of tokens (words, quoted identifiers, string literals,
punctuation and other characters, with comments and whitespace dropped) and, for a statement the lexer cannot
read to its end, the reason. Nothing is run and no database is touched. Not public: Murozond classifies the
statements, and it counts them (a step is one statement). Contract: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import enum
import re
import string
from collections.abc import Iterator
from dataclasses import dataclass

__all__ = ["ASCII_WHITESPACE", "Kind", "Statement", "Token", "split"]

# The reasons a statement is broken; Murozond reports them as its findings' reasons.
UNTERMINATED = "unterminated quote or comment"
META_COMMAND = "psql meta-command"

# PostgreSQL accepts every high-bit byte in a name, so every non-ASCII code point is a letter; `$` continues a word
_WORD = re.compile(r"[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_$\x80-\U0010ffff]*")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_\x80-\U0010ffff]*)?\$")  # `$1` is no tag
# String prefixes written directly before the quote: only E turns backslash escapes on (B, X, N and U& do not).
_STRING_PREFIXES = "BbEeNnXx"
_PUNCTUATION = "(),;"
# PostgreSQL's whitespace is these six characters; any other character, non-ASCII ones included, belongs to a name
ASCII_WHITESPACE = " \t\n\r\f\v"
# PostgreSQL folds the case of ASCII letters only (A-Z), whatever the encoding; `ı` and `ſ` stay as they are
_UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
# A `--` comment ends at the first newline or carriage return
_LINE_COMMENT = re.compile(r"--[^\n\r]*")
# What PostgreSQL joins two string literals with: a newline among spaces and `--` comments, then the next quote
_CONTINUATION = re.compile(r"[ \t\f]*(?:--[^\n\r]*)?[\n\r](?:[ \t\n\r\f\v]|--[^\n\r]*[\n\r])*'")


class Kind(enum.Enum):
    """What a token is. `BROKEN` only passes between the lexer and the splitter; no statement holds one."""

    WORD = "word"
    QUOTED = "quoted identifier"
    STRING = "string"
    PUNCT = "punctuation"
    OTHER = "other"
    BROKEN = "broken"


@dataclass(frozen=True)
class Token:
    """One token: its kind, its text and the number of parentheses open before it (a `)` counts after closing)."""

    kind: Kind
    text: str
    depth: int = 0

    @property
    def keyword(self) -> str:
        """The text of a word with its ASCII letters upper-cased, else "" (a quoted identifier is never a keyword)."""
        return self.text.translate(_UPPER) if self.kind is Kind.WORD else ""

    @property
    def is_dot(self) -> bool:
        """Whether this is the `.` that joins the parts of a name (a word after it is a name, never a keyword)."""
        return self.kind is Kind.OTHER and self.text == "."

    @property
    def identifier(self) -> str | None:
        """The name a word or quoted identifier stands for (a word's ASCII letters folded to lower case), else None."""
        if self.kind is Kind.WORD:
            return self.text.translate(_LOWER)
        return self.text if self.kind is Kind.QUOTED else None


@dataclass(frozen=True)
class Statement:
    """One statement's tokens; `broken` is the reason the lexer could not read it to its end, else None."""

    tokens: tuple[Token, ...]
    broken: str | None = None


def split(sql: str) -> list[Statement]:
    """Split SQL on the `;` outside quotes, comments, parentheses and a `BEGIN ATOMIC ... END` body.

    A string literal split by a newline (`'a'` newline `'b'`) is one token. An empty statement (`;;`) is dropped.
    Text after an unterminated quote or comment belongs to the statement that opened it.
    A backslash outside a quote (a psql meta-command) marks its statement broken and lexing goes on.
    """
    statements: list[Statement] = []
    tokens: list[Token] = []
    broken: str | None = None
    parens = 0  # open parentheses
    atomic = 0  # nesting of CASE and END inside a BEGIN ATOMIC body; 0 outside one
    for kind, text in _lex(sql):
        if kind is Kind.BROKEN:
            broken = broken or text
            continue
        if kind is Kind.PUNCT and text == ";" and parens == 0 and atomic == 0:
            if tokens or broken:
                statements.append(Statement(tuple(tokens), broken))
            tokens, broken = [], None
            continue
        if kind is Kind.PUNCT and text == ")":
            parens = max(0, parens - 1)
        token = Token(kind, text, parens)
        if kind is Kind.PUNCT and text == "(":
            parens += 1
        if atomic:
            if not (tokens and tokens[-1].is_dot):  # `r.case` is a name, not a keyword
                atomic += (token.keyword == "CASE") - (token.keyword == "END")
        elif token.keyword == "ATOMIC" and parens == 0 and tokens and tokens[-1].keyword == "BEGIN":
            atomic = _defines_routine(tokens)
        tokens.append(token)
    if tokens or broken:
        statements.append(Statement(tuple(tokens), broken))
    return statements


def _defines_routine(tokens: list[Token]) -> int:
    """1 when the statement so far starts `CREATE [OR REPLACE] FUNCTION` or `PROCEDURE` and its `BEGIN` is at depth 0.

    Only that has a `BEGIN ATOMIC` body, and it is decided by position: a word that merely reads like one, such as a
    view named `function`, opens nothing.
    """
    start = 3 if [t.keyword for t in tokens[1:3]] == ["OR", "REPLACE"] else 1
    kind = tokens[start].keyword if start < len(tokens) else ""
    return int(tokens[0].keyword == "CREATE" and kind in ("FUNCTION", "PROCEDURE") and tokens[-1].depth == 0)


def _lex(sql: str) -> Iterator[tuple[Kind, str]]:
    """Yield the tokens of the SQL, skipping whitespace and comments; a `BROKEN` token carries a reason."""
    pos = 0
    while pos < len(sql):
        kind, text, pos = _scan(sql, pos)
        if kind is not None:
            yield kind, text


def _scan(sql: str, pos: int) -> tuple[Kind | None, str, int]:
    """Read the token at *pos*: its kind (None for whitespace and comments), its text and where it ends."""
    ch = sql[pos]
    if ch in ASCII_WHITESPACE:
        return None, "", pos + 1
    if comment := _LINE_COMMENT.match(sql, pos):
        return None, "", comment.end()
    if sql.startswith("/*", pos):
        end = _comment_end(sql, pos)
        return (None, "", end) if end >= 0 else (Kind.BROKEN, UNTERMINATED, len(sql))
    if ch == "'":
        return _closed(Kind.STRING, sql, pos, _string_end(sql, pos, backslash=False))
    if ch == '"':
        kind, text, end = _closed(Kind.QUOTED, sql, pos, _quoted_end(sql, pos, '"', backslash=False))
        return kind, (text[1:-1].replace('""', '"') if kind is Kind.QUOTED else text), end
    if word := _WORD.match(sql, pos):
        return _word(sql, pos, word.end())
    if tag := _DOLLAR_TAG.match(sql, pos):
        close = sql.find(tag.group(), tag.end())
        return _closed(Kind.STRING, sql, pos, -1 if close < 0 else close + len(tag.group()))
    if ch == "\\":
        return Kind.BROKEN, META_COMMAND, pos + 1
    return (Kind.PUNCT if ch in _PUNCTUATION else Kind.OTHER), ch, pos + 1


def _word(sql: str, start: int, end: int) -> tuple[Kind, str, int]:
    """A word, or the string or name it prefixes (`E'..'`, `B'..'`, `U&'..'`, `U&".."`), read as one token.

    A `U&".."` name keeps its text as written, `U&` and quotes included, so it never equals a plain name.
    """
    word = sql[start:end]
    if len(word) == 1 and sql.startswith("'", end) and word in _STRING_PREFIXES:
        return _closed(Kind.STRING, sql, start, _string_end(sql, end, backslash=word in "Ee"))
    if len(word) == 1 and word in "Uu" and sql.startswith("&'", end):
        return _closed(Kind.STRING, sql, start, _string_end(sql, end + 1, backslash=False))
    if len(word) == 1 and word in "Uu" and sql.startswith('&"', end):
        return _closed(Kind.QUOTED, sql, start, _quoted_end(sql, end + 1, '"', backslash=False))
    return Kind.WORD, word, end


def _closed(kind: Kind, sql: str, start: int, end: int) -> tuple[Kind, str, int]:
    """The token from *start* to *end*, or, when *end* is -1 (never closed), a broken one that swallows the rest."""
    if end < 0:
        return Kind.BROKEN, UNTERMINATED, len(sql)
    return kind, sql[start:end], end


def _string_end(sql: str, start: int, *, backslash: bool) -> int:
    """The index after the string literal opening at *start*, or -1; strings split by a newline are one literal.

    The continuation is read like the string it continues, so an `E` string keeps its backslash escapes.
    """
    end = _quoted_end(sql, start, "'", backslash=backslash)
    while end >= 0 and (joint := _CONTINUATION.match(sql, end)):
        end = _quoted_end(sql, joint.end() - 1, "'", backslash=backslash)
    return end


def _quoted_end(sql: str, start: int, quote: str, *, backslash: bool) -> int:
    """The index after the closing *quote* of the quoted text opening at *start*, or -1; a doubled quote escapes."""
    pos = start + 1
    while pos < len(sql):
        if backslash and sql[pos] == "\\":
            pos += 2
        elif sql[pos] != quote:
            pos += 1
        elif sql.startswith(quote, pos + 1):
            pos += 2
        else:
            return pos + 1
    return -1


def _comment_end(sql: str, start: int) -> int:
    """The index after the `*/` closing the (nested) block comment opening at *start*, or -1."""
    depth = 0
    pos = start
    while pos < len(sql):
        if sql.startswith("/*", pos):
            depth += 1
            pos += 2
        elif sql.startswith("*/", pos):
            depth -= 1
            pos += 2
            if depth == 0:
                return pos
        else:
            pos += 1
    return -1
