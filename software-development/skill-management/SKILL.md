---
name: skill-management
description: "Guidelines for maintaining and updating Hermes-Agent skill library."
version: 0.1.0
author: User (provided), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skill-management, maintenance, hermes-agent]
    related_skills: [hermes-agent-skill-authoring]
---

# Skill Management Skill

## Overview

This skill defines the **standard workflow** for creating, updating, and extending Hermes-Agent class‑level skills. It captures permanent user preferences, proven procedures, and common pitfalls to ensure future sessions can modify the skill library correctly on the first try.

## When to Use

- Adding a new class‑level skill that describes a repeatable workflow.
- Updating an existing in‑repo skill with new rules, corrections, or additional references.
- Adding supporting files (`references/`, `templates/`, `scripts/`) to an existing skill.
- Incorporating user‑stated style or workflow preferences into a skill.

*Do not use for* one‑off session notes, transient error logs, or environment‑specific fixes (e.g., missing binaries).

## Prerequisites

- Access to the local Hermes skill directory (e.g., `C:\Users\flash\AppData\Local\hermes\skills`).
- Familiarity with the Hermes `skill_manage` and `write_file` tools.
- No pending uncommitted changes that would conflict with new files.

## Procedure

1. **Identify the target skill** – run `skills_list` and `skill_view` to locate the correct skill directory.
2. **Read the current content** – `skill_view(name='<skill>')` to obtain the latest SKILL.md and any linked files.
3. **Decide the action**:
   - *Patch*: small fix → `skill_manage(action='patch', ...)` or `patch`.
   - *Rewrite*: major change → `write_file` with full content.
   - *Add support file*: `write_file` with `path='software-development/skill-management/references/<topic>.md'` (or `templates/`, `scripts/`).
4. **Update the frontmatter** – ensure required fields (`name`, `description`, `version`, `author`, `license`, `platforms`, `metadata.hermes.{tags, related_skills}`) are present and conform to hardline rules (description ≤60 chars, author credits human first).
5. **Add or modify body sections** – follow the standard order: Overview, When to Use, Prerequisites, Procedure, Pitfalls, Verification.
6. **Validate locally** – run a quick Python script to check frontmatter syntax, description length, and that any `related_skills` exist.
7. **Create/Update supporting files** if needed, linking them from the SKILL.md body.
8. **Commit** – add the changed files to git, create a PR, and run the repo’s docs generator.

## Pitfalls

- **Editing a protected skill** – attempts to patch bundled or pinned skills will be rejected. Detect protection by checking `skill_view` metadata or the skill’s location under `~/.hermes/skills` vs repository paths.
- **Skipping the read‑before‑write step** – `skill_manage` requires a fresh `skill_view` load; forgetting this yields a `read_before_write` error.
- **Violating frontmatter limits** – description longer than 60 characters, missing required fields, or author only listing "Hermes Agent" will cause review rejection.
- **Using absolute machine paths** – all paths must be repository‑relative; hard‑coded `/home/...` or `C:\...` break portability.
- **Leaving duplicated rules** – before adding a new rule, search the existing skill (`search_files`) to consolidate with existing wording.
- **Neglecting verification** – always include a verification checklist; otherwise the skill is considered incomplete.

## Verification

- [ ] `skill_view` succeeds and shows updated content.
- [ ] Frontmatter passes YAML parsing and adheres to hardline limits.
- [ ] All `related_skills` entries resolve to existing in‑repo skills.
- [ ] Supporting files (if any) are present and referenced.
- [ ] Tests (if applicable) pass and docs are regenerated without unrelated diffs.

---

*This skill itself was created following the guidelines it now encodes.*