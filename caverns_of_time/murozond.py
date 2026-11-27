"""Murozond, the destructive-change guard's classifier: how risky is a step's SQL to run on a live database.

Input: SQL text. Output: a `Classification`, the worst `Risk` found and one `Finding` per risky statement or
action, each naming its statement and a fixed keyword reason, never text of the SQL. It reads keywords, not a parse
tree, runs nothing and imports no database driver and not Nozdormu. Contract: `caverns_of_time/README.md`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from caverns_of_time._sql import Kind, Statement, Token, split

__all__ = ["Classification", "Finding", "Risk", "classify"]


class Risk(enum.StrEnum):
    """How risky SQL is. In severity order, worst first: destructive, lock hazard, unknown, safe."""

    SAFE = "safe"
    DESTRUCTIVE = "destructive"
    LOCK_HAZARD = "lock_hazard"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    """One thing the classifier flagged: its risk (never safe), the 1-based statement and a fixed keyword reason."""

    risk: Risk
    statement: int
    reason: str


@dataclass(frozen=True)
class Classification:
    """The risk of some SQL, the worst finding's (safe without findings), and the findings in statement order."""

    risk: Risk
    findings: tuple[Finding, ...]


Verdict = tuple[Risk, str]  # a risk and its reason, before the statement is numbered
Name = tuple[str, ...]  # a table name as written: its dotted parts, unquoted words folded to lower case
Handler = Callable[[Sequence[Token], set[Name]], list[Verdict]]

_WORST_FIRST = (Risk.DESTRUCTIVE, Risk.LOCK_HAZARD, Risk.UNKNOWN, Risk.SAFE)
_NO_STATEMENT = "no statement"
_UNRECOGNISED = "unrecognised statement"

# The object types a DROP or ALTER names, as keyword phrases; a reason spells the longest one that matches, so a name
# that happens to be such a word (`DROP TABLE data`) is never read as part of the type.
_OBJECT_TYPES = tuple(
    sorted(
        (
            tuple(phrase.split())
            for phrase in (
                "TABLE|VIEW|MATERIALIZED VIEW|INDEX|SCHEMA|SEQUENCE|FUNCTION|PROCEDURE|ROUTINE|TYPE|DOMAIN|TRIGGER|"
                "EVENT TRIGGER|POLICY|EXTENSION|DATABASE|ROLE|USER|USER MAPPING|GROUP|OWNED|FOREIGN TABLE|"
                "FOREIGN DATA WRAPPER|SERVER|RULE|AGGREGATE|OPERATOR|OPERATOR CLASS|OPERATOR FAMILY|COLLATION|"
                "CONVERSION|PUBLICATION|SUBSCRIPTION|STATISTICS|TEXT SEARCH CONFIGURATION|TEXT SEARCH DICTIONARY|"
                "TEXT SEARCH PARSER|TEXT SEARCH TEMPLATE|ACCESS METHOD|CAST|TRANSFORM|LANGUAGE|PROCEDURAL LANGUAGE|"
                "TABLESPACE|LARGE OBJECT|DEFAULT PRIVILEGES"
            ).split("|")
        ),
        key=len,
        reverse=True,
    )
)
# A DROP that only removes a column's or domain's property, not an object.
_HARMLESS_DROPS = frozenset({"DEFAULT", "NOT", "EXPRESSION", "IDENTITY"})
_CONSTRAINT_STARTS = frozenset({"CONSTRAINT", "CHECK", "FOREIGN", "PRIMARY", "UNIQUE", "EXCLUDE"})
_CREATE_MODIFIERS = frozenset({"GLOBAL", "LOCAL", "TEMP", "TEMPORARY", "UNLOGGED", "UNIQUE", "FOREIGN"})
_ON_WORDS = frozenset({"TRUE", "ON"})  # the only values read as turning a boolean option on (the rest: off)
_ON_STRINGS = frozenset({"'true'", "'on'", "'1'"})
_SET_HAZARDS = frozenset({"TABLESPACE", "LOGGED", "UNLOGGED"})
# `ADD COLUMN` definitions that build an index or rewrite the table: the phrase and its reason.
_COLUMN_HAZARDS = (
    (("PRIMARY", "KEY"), "ADD COLUMN with PRIMARY KEY"),
    (("UNIQUE",), "ADD COLUMN with UNIQUE"),
    (("STORED",), "ADD COLUMN GENERATED STORED"),
)
_TRANSACTION_CONTROL = ("BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "PREPARE")
_SAFE_LEADING = frozenset("INSERT UPDATE GRANT REVOKE COMMENT SET RESET ANALYZE ANALYSE".split())
_SETTINGS = frozenset({"SET", "RESET"})  # they may change what an unqualified name refers to
# Statements whose leading keyword alone decides them.
_FIXED: dict[str, Verdict] = {
    "TRUNCATE": (Risk.DESTRUCTIVE, "TRUNCATE"),
    "DELETE": (Risk.DESTRUCTIVE, "DELETE"),
    "CLUSTER": (Risk.LOCK_HAZARD, "CLUSTER"),
    "LOCK": (Risk.LOCK_HAZARD, "LOCK"),
    "DO": (Risk.UNKNOWN, "DO block"),
    "CALL": (Risk.UNKNOWN, "CALL"),
    "EXECUTE": (Risk.UNKNOWN, "EXECUTE"),
    "SELECT": (Risk.UNKNOWN, "SELECT"),
    "WITH": (Risk.UNKNOWN, "WITH"),
    **dict.fromkeys(_TRANSACTION_CONTROL, (Risk.UNKNOWN, "transaction control")),
}


def classify(sql: str) -> Classification:
    """Classify the SQL: the worst risk of its statements and one finding per risky statement or ALTER action.

    A statement that cannot be read to its end, and SQL with no statement, are unknown. A table created earlier in
    the SQL (not by `IF NOT EXISTS`, which may find an old one) is new, so indexing or altering it later in the same
    SQL is safe (names compared as written, so `t` and `public.t` differ) until an `ALTER ... RENAME TO`, a
    `SET SCHEMA`, or a `SET` or `RESET` statement in the SQL, which may put an old table under a created name; an
    `ATTACH PARTITION` is never exempt and ends the exemption of its table. Strings are read as in
    `standard_conforming_strings = on`.
    """
    statements = split(sql)
    created: set[Name] = set()
    findings = [
        Finding(risk, number, reason)
        for number, statement in enumerate(statements, start=1)
        for risk, reason in _verdicts(statement, created)
    ]
    if not statements:
        findings.append(Finding(Risk.UNKNOWN, 1, _NO_STATEMENT))
    worst = min((f.risk for f in findings), key=_WORST_FIRST.index, default=Risk.SAFE)
    return Classification(worst, tuple(findings))


# --- one statement -----------------------------------------------------------------------


def _verdicts(statement: Statement, created: set[Name]) -> list[Verdict]:
    """The verdicts of one statement (none when it is safe); remembers the tables it creates in *created*."""
    if statement.broken:
        return [(Risk.UNKNOWN, statement.broken)]
    tokens = statement.tokens
    head = tokens[0].keyword
    if head in _FIXED:
        return [_FIXED[head]]
    if head in _SAFE_LEADING:
        if head in _SETTINGS:
            created.clear()
        return []
    handler = _HANDLERS.get(head)
    return handler(tokens, created) if handler else [(Risk.UNKNOWN, _UNRECOGNISED)]


def _drop(tokens: Sequence[Token], _created: set[Name]) -> list[Verdict]:
    return [(Risk.DESTRUCTIVE, " ".join(["DROP", *_object_words(tokens, 1)]))]


def _merge(tokens: Sequence[Token], _created: set[Name]) -> list[Verdict]:
    return [(Risk.DESTRUCTIVE, "MERGE with DELETE") if _has(tokens, "DELETE") else (Risk.UNKNOWN, "MERGE")]


def _copy(tokens: Sequence[Token], _created: set[Name]) -> list[Verdict]:
    """`COPY`: a DELETE or MERGE in the parenthesised query is destructive, PROGRAM runs a server command (unknown)."""
    query: Sequence[Token] = []
    if len(tokens) > 1 and _is_punct(tokens[1], "("):
        query = tokens[2 : _group_end(tokens)]
    verdicts = [(Risk.DESTRUCTIVE, f"COPY with {word}") for word in ("DELETE", "MERGE") if _has_keyword(query, word)]
    if _has_keyword(tokens, "PROGRAM", top=True):
        verdicts.append((Risk.UNKNOWN, "COPY PROGRAM"))
    return verdicts


def _group_end(tokens: Sequence[Token]) -> int:
    """The index of the `)` closing the parenthesised group that opens at index 1 (the end when it never closes)."""
    return next((i for i in range(2, len(tokens)) if _is_punct(tokens[i], ")") and tokens[i].depth == 0), len(tokens))


def _reindex(tokens: Sequence[Token], _created: set[Name]) -> list[Verdict]:
    """`REINDEX [(options)] target [CONCURRENTLY] name`: safe when CONCURRENTLY is on, by the keyword or the option."""
    rest = tokens[1:]
    option = None  # the last CONCURRENTLY option of the list, whose value counts
    if len(tokens) > 1 and _is_punct(tokens[1], "("):
        close = _group_end(tokens)
        options = [o for o in _split_commas(tokens[2:close], depth=1) if o and o[0].keyword == "CONCURRENTLY"]
        option = options[-1][1:] if options else None
        rest = tokens[close + 1 :]
    if (option is not None and _option_is_on(option)) or _has_keyword(rest, "CONCURRENTLY", top=True):
        return []
    return [(Risk.LOCK_HAZARD, "REINDEX without CONCURRENTLY")]


def _option_is_on(value: Sequence[Token]) -> bool:
    """Whether the value of a boolean option turns it on: none, TRUE, ON, 1, or the string 'true', 'on' or '1'."""
    if not value:
        return True
    if len(value) > 1:
        return False
    token = value[0]
    if token.kind is Kind.WORD:
        return token.keyword in _ON_WORDS
    if token.kind is Kind.STRING:
        return token.text.lower() in _ON_STRINGS
    return token.kind is Kind.OTHER and token.text == "1"


def _refresh(tokens: Sequence[Token], _created: set[Name]) -> list[Verdict]:
    if not _at(tokens, 0, "REFRESH", "MATERIALIZED", "VIEW"):
        return [(Risk.UNKNOWN, _UNRECOGNISED)]
    if _has_keyword(tokens, "CONCURRENTLY"):
        return []
    return [(Risk.LOCK_HAZARD, "REFRESH MATERIALIZED VIEW without CONCURRENTLY")]


def _vacuum(tokens: Sequence[Token], _created: set[Name]) -> list[Verdict]:
    return [(Risk.LOCK_HAZARD, "VACUUM FULL") if _has(tokens, "FULL") else (Risk.UNKNOWN, "VACUUM")]


def _create(tokens: Sequence[Token], created: set[Name]) -> list[Verdict]:
    """CREATE: an index without CONCURRENTLY on an older table is a hazard; a table is remembered as created."""
    i = 3 if _at(tokens, 1, "OR", "REPLACE") else 1
    while i < len(tokens) and tokens[i].keyword in _CREATE_MODIFIERS:
        i += 1
    kind = tokens[i].keyword if i < len(tokens) else ""
    if kind == "INDEX":
        return _create_index(tokens[i + 1 :], created)
    if kind == "TABLE" and not _at(tokens, i + 1, "IF", "NOT", "EXISTS"):  # IF NOT EXISTS may find an old table
        name, _ = _name(tokens, i + 1)
        if name:
            created.add(name)
    return []


def _create_index(tokens: Sequence[Token], created: set[Name]) -> list[Verdict]:
    """The tokens after `CREATE [UNIQUE] INDEX`: safe when CONCURRENTLY, or on a table created in the same SQL."""
    on = next((i for i, t in enumerate(tokens) if t.keyword == "ON" and t.depth == 0), len(tokens))
    if _has_keyword(tokens[:on], "CONCURRENTLY"):
        return []
    table, _ = _name(tokens, on + (2 if _at(tokens, on + 1, "ONLY") else 1))
    return [] if table in created else [(Risk.LOCK_HAZARD, "CREATE INDEX without CONCURRENTLY")]


# --- ALTER -------------------------------------------------------------------------------


def _alter(tokens: Sequence[Token], created: set[Name]) -> list[Verdict]:
    is_table = _at(tokens, 1, "TABLE") or _at(tokens, 1, "FOREIGN", "TABLE")
    verdicts = _alter_table(tokens, created) if is_table else _alter_other(tokens)
    if _has(tokens, "RENAME", "TO") or _has(tokens, "SET", "SCHEMA"):  # a table may now be an old one under a new name
        created.clear()
    return verdicts


def _alter_table(tokens: Sequence[Token], created: set[Name]) -> list[Verdict]:
    """ALTER [FOREIGN] TABLE: one verdict per risky action; for a table created in the same SQL only ATTACH PARTITION
    counts, and it ends the table's exemption (the partition attached is an old table)."""
    i = 3 if tokens[1].keyword == "FOREIGN" else 2
    if _at(tokens, i, "ALL", "IN"):
        return [(Risk.LOCK_HAZARD, "SET TABLESPACE")]
    i += 2 if _at(tokens, i, "IF", "EXISTS") else 0
    i += 1 if _at(tokens, i, "ONLY") else 0
    name, i = _name(tokens, i)
    i += 1 if i < len(tokens) and tokens[i].text == "*" else 0
    actions = _split_commas(tokens[i:])
    if name in created:
        actions = [action for action in actions if _at(action, 0, "ATTACH", "PARTITION")]
        if actions:
            created.discard(name)
    for action in actions:  # a created table made a child is no longer a new table
        if _at(action, 0, "INHERIT"):
            created.discard(_name(action, 1)[0])
    verdicts = (_table_action(action) for action in actions)
    return [verdict for verdict in verdicts if verdict]


def _table_action(action: Sequence[Token]) -> Verdict | None:
    """The verdict of one ALTER TABLE action, None when it is safe."""
    words = [t.keyword for t in action] + [""]
    head = words[0]
    if head == "DROP":
        what = "CONSTRAINT" if words[1] == "CONSTRAINT" else "COLUMN"
        return Risk.DESTRUCTIVE, f"ALTER TABLE DROP {what}"
    if head == "ALTER":
        return _alter_column(words)
    if head == "ADD":
        return _add(action, words)
    if head == "SET" and words[1] in _SET_HAZARDS:
        return Risk.LOCK_HAZARD, f"SET {words[1]}"
    if head == "ATTACH" and words[1] == "PARTITION":
        return Risk.LOCK_HAZARD, "ATTACH PARTITION"
    if head == "DETACH" and words[1] == "PARTITION":
        _, after = _name(action, 2)  # CONCURRENTLY and FINALIZE only count after the partition's name
        if not {"CONCURRENTLY", "FINALIZE"} & set(words[after:]):
            return Risk.LOCK_HAZARD, "DETACH PARTITION without CONCURRENTLY"
    return None


def _alter_column(words: list[str]) -> Verdict | None:
    """`ALTER [COLUMN] <name> ...` as keywords; the column's name is skipped by position (it may be quoted)."""
    if words[1] == "CONSTRAINT":
        return None
    after = words[(3 if words[1] == "COLUMN" else 2) :] + ["", "", ""]
    if after[0] == "TYPE" or after[:3] == ["SET", "DATA", "TYPE"]:
        return Risk.LOCK_HAZARD, "ALTER COLUMN TYPE"
    if after[:3] == ["SET", "NOT", "NULL"]:
        return Risk.LOCK_HAZARD, "SET NOT NULL"
    return None


def _add(action: Sequence[Token], words: list[str]) -> Verdict | None:
    """`ADD ...`: a table constraint needs NOT VALID (or an existing index), a column must not build one."""
    if words[1] in _CONSTRAINT_STARTS or words[1:3] == ["NOT", "NULL"]:  # `ADD NOT NULL a` (PostgreSQL 18)
        # USING INDEX TABLESPACE is a storage option of a new index, not an existing one
        existing_index = _has(action, "USING", "INDEX", top=True) and not _has(action, "USING", "INDEX", "TABLESPACE")
        keyed = bool({"PRIMARY", "UNIQUE"} & set(words))
        if _has(action, "NOT", "VALID", top=True) or (keyed and existing_index):
            return None
        return Risk.LOCK_HAZARD, "ADD CONSTRAINT without NOT VALID"
    for phrase, reason in _COLUMN_HAZARDS:
        if _has(action, *phrase):
            return Risk.LOCK_HAZARD, reason
    return None


def _alter_other(tokens: Sequence[Token]) -> list[Verdict]:
    """ALTER of any other object: a DROP action is destructive, a SET TABLESPACE a lock hazard."""
    objects = _object_words(tokens, 1)
    verdicts: list[Verdict] = []
    for action in _split_commas(tokens[1 + len(objects) :]):
        drop = next((i for i, t in enumerate(action) if t.keyword == "DROP" and t.depth == 0), None)
        following = action[drop + 1].keyword if drop is not None and drop + 1 < len(action) else ""
        if drop is not None and following not in _HARMLESS_DROPS:
            verdicts.append((Risk.DESTRUCTIVE, " ".join(["ALTER", *objects, "DROP"])))
        if _has(action, "SET", "TABLESPACE", top=True):
            verdicts.append((Risk.LOCK_HAZARD, "SET TABLESPACE"))
    return verdicts


# --- reading tokens ----------------------------------------------------------------------

_HANDLERS: dict[str, Handler] = {
    "DROP": _drop,
    "MERGE": _merge,
    "COPY": _copy,
    "CREATE": _create,
    "ALTER": _alter,
    "REINDEX": _reindex,
    "REFRESH": _refresh,
    "VACUUM": _vacuum,
}


def _at(tokens: Sequence[Token], start: int, *phrase: str) -> bool:
    """Whether the keywords of *phrase* are the tokens from *start*, in a row."""
    window = tokens[start : start + len(phrase)]
    return len(window) == len(phrase) and all(t.keyword == word for t, word in zip(window, phrase, strict=True))


def _has(tokens: Sequence[Token], *phrase: str, top: bool = False) -> bool:
    """Whether the keywords of *phrase* appear in a row among the tokens (all outside parentheses when *top*)."""
    return any(
        _at(tokens, i, *phrase) and (not top or all(t.depth == 0 for t in tokens[i : i + len(phrase)]))
        for i in range(len(tokens))
    )


def _is_punct(token: Token, text: str) -> bool:
    return token.kind is Kind.PUNCT and token.text == text


def _has_keyword(tokens: Sequence[Token], word: str, *, top: bool = False) -> bool:
    """Whether the keyword *word* is among the tokens (outside parentheses when *top*), not as a name (`s.word`)."""
    return any(
        token.keyword == word and (not top or token.depth == 0) and not (i and tokens[i - 1].is_dot)
        for i, token in enumerate(tokens)
    )


def _object_words(tokens: Sequence[Token], start: int) -> tuple[str, ...]:
    """The object type named from *start* (`MATERIALIZED VIEW`), as keywords; empty when none is."""
    return next((phrase for phrase in _OBJECT_TYPES if _at(tokens, start, *phrase)), ())


def _name(tokens: Sequence[Token], start: int) -> tuple[Name, int]:
    """The dotted name at *start* as written, and the index after it."""
    parts: list[str] = []
    i = start
    while i < len(tokens) and (part := tokens[i].identifier) is not None:
        parts.append(part)
        i += 1
        if i + 1 < len(tokens) and tokens[i].is_dot:
            i += 1
        else:
            break
    if parts and i < len(tokens) and tokens[i].keyword == "UESCAPE":  # the escape character may differ: never a match
        return (), i + 2
    return tuple(parts), i


def _split_commas(tokens: Sequence[Token], depth: int = 0) -> list[Sequence[Token]]:
    """Split the tokens at the commas at parenthesis *depth* (outside parentheses by default)."""
    actions: list[Sequence[Token]] = []
    begin = 0
    for i, token in enumerate(tokens):
        if _is_punct(token, ",") and token.depth == depth:
            actions.append(tokens[begin:i])
            begin = i + 1
    actions.append(tokens[begin:])
    return actions
