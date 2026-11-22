# caverns_of_time

## Purpose & scope

The importable package. It is generic: nothing here names a consumer or assumes a consumer's schema
(hard rule 1). Until Phase 1 it holds no component, only the version.

## Files & entry points

| File | Holds |
|---|---|
| `__init__.py` | `__version__`, read from the installed distribution's metadata |

## How it works

Nothing to describe yet. Each component arrives as one module in its roadmap phase (`ROADMAP.md`).

## Interfaces & contracts

- `caverns_of_time.__version__`: the version from `pyproject.toml`; `0+unknown` in a checkout that was
  never installed.
- No runtime dependency. Adding one is an ask-first decision.

## Extending

Follow the adding-a-component workflow of the development skill
(`.claude/skills/caverns-of-time-development/references/workflows/adding-a-component.md`).

## See also

`ROADMAP.md` · `.claude/skills/caverns-of-time-development/references/architecture.md`
