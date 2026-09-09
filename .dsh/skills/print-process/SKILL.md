---
name: print-process
description: Explain paper, color, finishing, binding, imposition, and print-mode trade-offs.
---

# Print Process

Use this skill when the user asks how to choose a paper, color process, finishing treatment, binding, or production mode.

## Explain, do not promise

Explain the trade-off between cost, appearance, color consistency, lead time, minimum quantity, and process risk. Distinguish gang-run (`合版`) and dedicated-run (`专版`) implications, and explain that reference estimates are not supplier quotes.

Use the versioned PrintOps knowledge manifest and include `knowledgeVersion` in the result. If a rule depends on a supplier's current capability, mark it for review instead of guessing.

## Output and boundaries

Return structured `patch` only when the user explicitly chooses a process; otherwise return explanations, questions, risks, and evidence. Never silently change paper, finishing, binding, or color fields. Never fabricate a live price or delivery date, write SQLite, or contact a supplier.
