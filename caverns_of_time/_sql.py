"""The SQL lexer and statement splitter shared by the components that read a step's SQL text.

Input: SQL text. Output: its statements, each a tuple of tokens (words, quoted identifiers, string literals,
punctuation and other characters, with comments and whitespace dropped) and, for a statement the lexer cannot
read to its end, the reason. Nothing is run and no database is touched. Not public: Murozond classifies the
statements, and Nozdormu will count them. Contract: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterator
from dataclasses import dataclass

__all__ = ["Kind", "Statement", "Token", "split"]

# The reasons a statement is broken; Murozond reports them as its findings' reasons.
UNTERMINATED = "unterminated quote or comment"
META_COMMAND = "psql meta-command"

_WORD = re.compile(r"[A-Za-z_\x80-￿][A-Za-z0-9_$\x80-￿]*")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\x80-￿][A-Za-z0-9_\x80-￿]*)?\$")  # `$1` is a parameter, not a tag
# String prefixes written directly before the quote: only E turns backslash escapes on (B, X, N and U& do not).
_STRING_PREFIXES = "BbEeNnXx"
_PUNCTUATION = "(),;"


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
        """The upper-cased text of a word, else "" (a quoted identifier is never a keyword)."""
        return self.text.upper() if self.kind is Kind.WORD else ""

    @property
    def identifier(self) -> str | None:
        """The name a word or quoted identifier stands for (a word folded to lower case), else None."""
        if self.kind is Kind.WORD:
            return self.text.lower()
        return self.text if self.kind is Kind.QUOTED else None


@dataclass(frozen=True)
class Statement:
    """One statement's tokens; `broken` is the reason the lexer could not read it to its end, else None."""

    tokens: tuple[Token, ...]
    broken: str | None = None


def split(sql: str) -> list[Statement]:
    """Split SQL on the `;` outside quotes, comments, parentheses and a `BEGIN ATOMIC ... END` body.

    An empty statement (`;;`) is dropped. Text after an unterminated quote or comment belongs to the statement that
    opened it. A backslash outside a quote (a psql meta-command) marks its statement broken and lexing goes on.
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
            atomic += (token.keyword == "CASE") - (token.keyword == "END")
        elif token.keyword == "ATOMIC" and tokens and tokens[-1].keyword == "BEGIN":
            atomic = 1
        tokens.append(token)
    if tokens or broken:
        statements.append(Statement(tuple(tokens), broken))
    return statements


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
    if ch.isspace():
        return None, "", pos + 1
    if sql.startswith("--", pos):
        end = sql.find("\n", pos)
        return None, "", len(sql) if end < 0 else end
    if sql.startswith("/*", pos):
        end = _comment_end(sql, pos)
        return (None, "", end) if end >= 0 else (Kind.BROKEN, UNTERMINATED, len(sql))
    if ch == "'":
        return _closed(Kind.STRING, sql, pos, _quoted_end(sql, pos, "'", backslash=False))
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
    """A word, or the string it prefixes (`E'..'`, `B'..'`, `U&'..'`), which is read as one string token."""
    word = sql[start:end]
    if len(word) == 1 and sql.startswith("'", end) and word in _STRING_PREFIXES:
        return _closed(Kind.STRING, sql, start, _quoted_end(sql, end, "'", backslash=word in "Ee"))
    if len(word) == 1 and word in "Uu" and sql.startswith("&'", end):
        return _closed(Kind.STRING, sql, start, _quoted_end(sql, end + 1, "'", backslash=False))
    return Kind.WORD, word, end


def _closed(kind: Kind, sql: str, start: int, end: int) -> tuple[Kind, str, int]:
    """The token from *start* to *end*, or, when *end* is -1 (never closed), a broken one that swallows the rest."""
    if end < 0:
        return Kind.BROKEN, UNTERMINATED, len(sql)
    return kind, sql[start:end], end


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
