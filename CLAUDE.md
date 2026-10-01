# CLAUDE.md — ICBM-NEW bootstrap index

Claude Code loads this file automatically. It is only the bootstrap and index: the operating rules
live under `documents/rules/` (ADR-0021 §3, Issue #151 §2) and are imported below, so they are
loaded with this file rather than merely referenced. Architecture and domain decisions stay owned
by their ADRs (`documents/decisions/adr/`); nothing here copies them.

Former `CLAUDE.md` section numbers are kept inside the imported files; the section map is in
`documents/rules/README.md`, and every moved path is in `documents/reference/PATH_MIGRATION_MAP.md`.
Each imported rule body of former §1–§10 and §12 is the former `CLAUDE.md` section, unchanged
except for moved-path locators (ADR-0021 §4): `tests/contracts/test_repository_rules.py`
(`test_rule_bodies_are_the_former_claude_md_sections`) proves it against the pre-migration text,
and `test_claude_md_auto_loads_every_rule_body` proves that every rule body is imported here.
`documents/rules/README.md` is not a preserved body: it is the new index, which restates the former
intro and §13 read order with the moved locators and adds the section map. Former §11 (milestone
status) is roadmap content, pinned by the milestone agreement test.
`documents/rules/14-operating-authority.md` is not a former section either: it is the operating
rule of ADR-0022 (who decides what, and what is never asked of the user).

@documents/rules/README.md
@documents/rules/01-roles-and-exchange.md
@documents/rules/02-no-legacy.md
@documents/rules/03-ui-source.md
@documents/rules/04-runtime-stack.md
@documents/rules/05-architectural-rules.md
@documents/rules/06-immutable-domain-rules.md
@documents/rules/07-execution-safety.md
@documents/rules/08-git-conventions.md
@documents/rules/09-definition-of-done.md
@documents/rules/10-working-style.md
@documents/roadmap/CURRENT-MILESTONE.md
@documents/rules/12-first-vertical.md
@documents/rules/14-operating-authority.md
