# On-device templates

reMark ships with a YAML-driven template engine that renders
fillable PDFs, pushes them to your tablet, and — once you fill them
in — parses the handwriting back into structured frontmatter.

Templates live in two places:

- **Built-in**: under `src/templates/builtin/` (ships with the pip
  package). Always available, can be overridden by same-named user
  templates.
- **User**: the directory pointed at by `templates.user_templates_dir`
  in `config.yaml` (default `~/.remark-bridge/templates`). Edit these
  from the web UI at `/templates` or directly on disk.

## File shape

A minimal template is a flat YAML file:

```yaml
name: meeting
description: Structured notes for a meeting
title_prefix: "Meeting"
fields:
  - name: date
    heading: Date
    type: date
    hint: YYYY-MM-DD
  - name: attendees
    heading: Attendees
    type: list
  - name: decisions
    heading: Decisions
    type: list
  - name: actions
    heading: Action items
    type: checklist
```

### Field types

| `type`        | PDF layout                | Extraction                    |
|---------------|---------------------------|-------------------------------|
| `text`        | Three ruled lines         | All content under the heading |
| `list`        | Five `• ________`         | One item per non-empty line   |
| `checklist`   | Same as `list`            | Passed through to actions     |
| `date`        | One `____-____-____`      | First non-blank line          |

Each field accepts:

- `name` — key in the extracted frontmatter dict
- `heading` — rendered as H2 in the PDF; extraction matches by this
- `required` *(optional)* — advisory flag for UIs
- `hint` *(optional)* — grey helper text printed below the heading

## Conditional rendering with `when:`

A field may carry a `when:` expression. When the expression evaluates
to false against the values passed to `render_pdf()`, the field is
skipped entirely (no heading, no writing space). Use this to build
variants of one template instead of duplicating files.

```yaml
fields:
  - name: attendees
    heading: Attendees
    type: list
  - name: external_guests
    heading: External guests
    type: list
    when: "kind == 'external'"
```

### `when:` grammar

| Accepts                                                                              | Example                                     |
|--------------------------------------------------------------------------------------|---------------------------------------------|
| Equality / inequality                                                                | `kind == 'standup'`                         |
| Membership                                                                           | `'urgent' in tags`                          |
| Boolean combinators                                                                  | `kind == 'review' and not archived`         |
| Literals                                                                             | `active`, `5`, `'hello'`, `['a','b']`       |

Anything else — function calls, attribute access, subscripting, imports,
comprehensions — raises `ConditionError` and the template is refused.
Expressions are capped at 500 characters and 200 AST nodes.

Missing identifiers resolve to `None`, so `x == 'foo'` is simply
`False` against an empty context rather than a crash.

## Inheritance — `extends:` + `blocks:`

Define a reusable base with named blocks, then override them from
derived templates:

```yaml
# meeting.yaml (base)
name: meeting
fields:
  - name: date
    heading: Date
    type: date
  - name: summary
    heading: Summary
    type: text
    block: body
  - name: actions
    heading: Action items
    type: checklist
```

```yaml
# standup.yaml
name: standup
extends: meeting
blocks:
  body:
    - name: yesterday
      heading: Yesterday
      type: text
    - name: today
      heading: Today
      type: text
    - name: blockers
      heading: Blockers
      type: text
```

Resolution order:

1. Inherit the parent's field list.
2. Replace any parent field whose `block` matches a key in the child's
   `blocks:` — the child's block entries take its position.
3. Append any fields in the child's top-level `fields:` that don't
   belong to a named block.

Cycles are detected (`a extends b extends a`) and the offending
template is logged + skipped; the rest of the engine stays usable.

## The web editor

`/templates` lists every template the engine can resolve, plus any
YAML files sitting in the user dir that failed to parse. Clicking a
template opens a CodeMirror 6 editor with:

- **Save** — validates via the same parser the engine uses, writes
  back to the user dir atomically.
- **Preview PDF** — posts the current editor contents to
  `/templates/<name>/preview`, gets back rendered PDF bytes, opens
  them in a new tab. No permanent state is written.

Validation failures re-render the form with the error message inline
so users can correct YAML mistakes without losing their work.

## CLI

```bash
remark-bridge template list
remark-bridge template push meeting
```

Pushed templates land under the `templates.target_folder` on the
tablet (default `Templates`). When a user fills one in and it syncs
back, the engine detects it (via frontmatter `template:` key or a
heading match) and extracts the fields into the note's frontmatter.
