---
title: "Actions: {{source_note}}"
date: {{date}}
source: remarkable
type: actions
linked_note: "[[{{source_note}}]]"
---

# Action Items — {{source_note}}

{{#actions}}
- [ ] **{{task}}** {{#assignee}}(@{{assignee}}){{/assignee}} {{#deadline}}📅 {{deadline}}{{/deadline}}
  - Priority: {{priority}}
  - Context: *{{source_context}}*
{{/actions}}
