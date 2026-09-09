---
name: print-quote-handoff
description: Prepare a traceable print-order handoff or quote draft before human confirmation.
---

# Print Quote Handoff

Use this skill only after the order has been validated and the user is ready to compare a handoff or quote request.

## Checklist

- Show missing fields, item IDs, selected process, file/preflight status, capability matches, assumptions, and knowledge versions.
- Keep reference estimates visibly separate from supplier quotes.
- For multi-product orders, operate on one item at a time and preserve stable `itemId` values.
- Explain exactly what a human would be confirming, including price, delivery, file status, and target platform.

## Hard boundaries

Return a local draft and structured risks; do not send email, HTTP, CUPS, Printful, or supplier requests. Do not mark an order confirmed. Any order change invalidates a stale draft and requires revalidation. Do not expose credentials or raw file contents in the output.
