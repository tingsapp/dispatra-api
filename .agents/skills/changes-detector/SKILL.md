---
name: changes-detector
description: Use for code or behavior changes in Dispatra to find affected producers, consumers, contracts, state, and tests, then update connected parts together.
---

# Changes Detector

For each code or behavior change:

1. Identify the changed behavior or contract. Search for every place that creates, reads, validates, displays, stores, or transmits the affected data. Include alternate UI entry points and shared types where relevant.
2. Update the related code together. Keep UI labels, forms, calculations, API schemas, persistence, defaults, migrations, and documentation consistent when the change reaches them. Preserve existing saved data when a change affects its shape or meaning.
3. Check the connected flows with focused tests and the applicable project checks. Follow the local `AGENTS.md` instructions for required verification.
4. Review the final diff for stale references or incomplete paths. State any remaining dependency or limitation accurately.

Follow actual dependencies; do not change unrelated code just because it is nearby.
