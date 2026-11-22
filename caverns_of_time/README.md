# caverns_of_time

## Purpose & scope

The importable package. It is generic: nothing here names a consumer or assumes a consumer's schema
(hard rule 1). It holds the version, the shared exceptions and, so far, one component: Nozdormu, the
migration ledger (in progress; this file describes what is built).

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | `__version__`, read from the installed distribution's metadata |
| `errors.py` | The exception types, all in one module |
| `nozdormu.py` | Nozdormu: the `Step` and `Changeset` model, the checksum and the loaders |

## How it works

Each component arrives as one module in its roadmap phase (`ROADMAP.md`).

**Nozdormu, the model.** A `Step` is a label, SQL and an optional check; a `Changeset` is a name and an
ordered list of steps. Both are frozen and validated when built. A changeset comes from a Python list or
from a directory (`Changeset.from_directory`): every `*.sql` file of that directory, not its
sub-directories, is a step in file-name order, labelled with the file name minus `.sql`;
`<label>.check.sql` is the check of the step `<label>.sql`. Other files are ignored. A step is also
readable from one file (`Step.from_file`).

## Interfaces & contracts

- `caverns_of_time.__version__`: the version from `pyproject.toml`; `0+unknown` in a checkout that was
  never installed.
- `Step(label, sql, check=None)`: the label is a non-empty single line without surrounding whitespace;
  the SQL is not blank. A check is SQL returning exactly one row with one boolean column, true when the
  step's effect is already present. `Step.from_file(path, *, label=None, check=None)` reads UTF-8 text.
- `Step.checksum`: the SHA-256 hex of the SQL as UTF-8 after line endings are normalised to `\n` and the
  whole text's leading and trailing whitespace is trimmed. It covers the SQL only, not the label, the
  check or the changeset name, so a comment change inside the SQL changes it and a re-indent of the
  whole file does not.
- `Changeset(name, steps)`: the name follows the label rules; at least one step; labels are unique. The
  name `caverns` is Caverns' own.
- Every invalid input raises `ChangesetError`, naming the offending value.
- No runtime dependency. Adding one is an ask-first decision.

## Extending

Follow the adding-a-component workflow of the development skill
(`.claude/skills/caverns-of-time-development/references/workflows/adding-a-component.md`).

## See also

`ROADMAP.md` · `.claude/skills/caverns-of-time-development/references/architecture.md`
