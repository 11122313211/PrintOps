---
name: print-specs
description: Complete category-specific paper-printing specifications and dimension semantics.
---

# Print Specifications

Use this skill after a product category is identified and the user needs help completing its required parameters.

## Dimension semantics

Keep these concepts separate: finished size, expanded size, die-cut size, and package length/width/height. For boxes and bags, preserve whether dimensions are inner or outer dimensions. Never silently relabel a three-dimensional package size as a flat finished size.

## Category guidance

Use the current PrintOps product profile for required keys. Typical categories include cards, leaflets, books/brochures, labels, boxes, bags, cups, posters, and display materials. Ask only for parameters relevant to the selected profile, such as label material/shape, box structure, bag handle, cup volume, or display substrate.

## Output and boundaries

Return `patch`, `evidence`, `confidence`, `questions`, `risks`, and `knowledgeVersion`. Validate `productSpecs` keys against the selected profile. Unknown keys must be rejected or returned as `rejectedFields`; they must not be written into the order.

Do not infer production values from a product name alone, and do not provide live pricing, capacity, or delivery guarantees. Do not write state or contact a supplier.
