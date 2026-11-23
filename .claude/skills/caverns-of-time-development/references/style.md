# Style — docstrings, comments, messages, CLI

Review-enforced style conventions; the hard rules R1–R9 and the simplicity audit live in
`references/conventions.md`.

## Comments

Comments explain **why**, not **what**.

- **Every module opens with a docstring** (checker rule C5): one line of purpose, then inputs, outputs
  and where the module sits. Public functions and classes carry a docstring; a one-liner suffices for
  a short helper.
- **Block comments** precede a logical block and state intent or the rule it implements, never a
  narration of the code.
- **`# NOTE:`** marks a deliberate divergence or non-obvious constraint; **`# WARNING:`** marks code
  that breaks if reordered or "cleaned up".
- Never restate the code (`# Loop over rows`) or leave vague placeholders (`# TODO: fix this`).
- Long narratives belong in the module README or the skill, with a pointer from the code.

## Messages

- **Library code never prints** (R2). It logs through `logging.getLogger(__name__)`; log messages
  carry no icons and no trailing full stop.
- **CLI status lines** go through the package's console helpers, one helper per level, with this icon
  vocabulary: ✅ success · ❌ error · ⚠️ warning · ℹ️ information. The text never repeats the level
  the icon already shows ("Warning:", "Error:").
- **One wording per situation:**

| Situation | Wording |
|---|---|
| Nothing to do | `Nothing to <verb> — <reason>.` |
| Confirmation declined | `Aborted — no changes were made.` |
| Task finished | `<Task> complete.` |
| A step failed | `Step failed: <label>` then the database error on the next line |

- A message names the object it is about (the step label, the epoch, the table), never "it".
- Errors go to stderr, results to stdout, so output can be piped.

## CLI

- One command, `chromie`, with subcommands; the entry module holds flow only (R9).
- **One flag name per concept**, reused by every subcommand:

| Flag | Meaning |
|---|---|
| `--dsn` | connection string (else the environment) |
| `--schema` | a schema name (repeatable where a command narrows to schemas) |
| `--changeset` | a changeset name |
| `--limit` | the most rows to show (0: all) |
| `--dry-run` | report what would change, write nothing |
| `--yes` | skip the confirmation (required unattended) |
| `--format` | output format |

- Help text is a lower-case fragment without a full stop.
- A destructive subcommand asks for confirmation interactively (default no) and requires `--yes`
  unattended.
- Exit codes are part of the public surface (ask-first): 0 success, 1 a finding or failure the command
  exists to report, 2 a usage or environment error.

The flag table and the exit codes are the conventions new subcommands follow; the subcommands
themselves are decided at each phase's sign-off (`ROADMAP.md`).
