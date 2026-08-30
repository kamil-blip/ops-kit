# platform/memory/ : the memory-file convention

This directory is the scaffold for the assistant's cross-session memory. It ships
empty on purpose: your system starts blank and fills from your own work. The value
here is the convention, which keeps memory retrievable instead of turning into a
pile of stale notes.

Note on location: Claude Code keeps per-project auto-memory in its own user data
directory (a folder derived from the project path). Use this scaffold as the
template for that directory, or point your setup at this one. Either way, one
directory is canonical; never maintain two copies.

## Core rules

1. **One fact per file.** Each file captures a single durable rule, preference, or
   reference fact. If you are tempted to add a second topic, make a second file.
2. **Typed filenames.** `<type>_<slug>.md`, where type is one of:
   - `feedback_` : a correction or preference from the operator ("do X, not Y")
   - `project_`  : state and decisions for a multi-session project
   - `reference_`: a stable how-to fact about a tool, API, account, or data source
   - `personal/` : non-work entries, kept in the subfolder so work exports can skip it
3. **Index discipline.** Every file gets exactly one line in `MEMORY.md` under its
   type section: `- [Short title](filename.md) : one-line gist`. MEMORY.md is an
   index only (no content) and stays under 150 lines. If it grows past that,
   consolidate or archive files rather than shrinking the summaries.
4. **One source of truth per topic.** Before creating a file, search for an
   existing one and update it. Near-duplicate files are worse than none: retrieval
   surfaces the stale twin.
5. **DB vs files.** Structured, queryable data (people, tasks, events, learnings
   with lifecycle) belongs in the database. Memory files are for prose guidance an
   assistant should read before acting.
6. **Memories are point-in-time.** Date your claims where it matters. When a fact
   changes, update or delete the file; do not append contradictions.

## File shape

Frontmatter, then a short structured body:

```markdown
---
name: kebab-case-imperative-name
description: "One or two sentences an assistant can act on without opening the file."
metadata:
  node_type: memory
  type: feedback          # feedback | project | reference | personal
  originSessionId: <session-uuid, optional>
---

**Rule:** The actionable instruction, stated first.

**Why:** The incident or reasoning behind it, with a date if known.

**How to apply:** Concrete steps, commands, or checks. Short.

**Cross-references:** [[other_memory_slug]], plus any code or doc paths.
```

The `description` field is what gets surfaced in listings and retrieval, so write
it as the instruction itself, not as a teaser.

## [[links]]

Cross-reference other memory files with `[[double_bracket_slugs]]` matching the
filename without extension. When you rename or retire a file, grep the directory
for its slug and fix every referrer in the same pass. A broken [[link]] is a bug.

## Retiring a memory

Do not silently delete load-bearing rules. Replace the file body with a one-line
pointer to whatever superseded it (dated), keep it for a grace period, then remove
the file and its index line together.

## Worked example (FICTIONAL)

`feedback_confirm_before_bulk_send.md`, indexed under Feedback as:

```markdown
- [Confirm scope before bulk sends](feedback_confirm_before_bulk_send.md) : "send to this person" never means "send to everyone"
```
