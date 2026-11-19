> **Regenerated.** The original markdown of this run was overwritten by the next run (the harness writes a fixed file name).
> This file was rebuilt from the run's own results JSON with the unchanged report code; the `Run:` line below is the run's time
> taken from the JSON file name. The numbers are the ones the original report showed (checked against it when it was read).

# Proposal evaluation gate (REAL provider)

Run: 2026-11-19 15:20 UTC. Gold items: 16 (13 edits, 3 drafts).

**Gate verdict: PASS: the schema-valid rate with one repair is 100% (line: 90%).**

| Metric | Value | Basis |
|---|---|---|
| Schema-valid WITHOUT repair | 94% (15/16) | measured |
| Schema-valid WITH one repair (published) | 100% (16/16) | measured |
| Proposals whose base hashes, anchors and replay resolve | 100% (16/16) | measured: the real capture, hashes and the real engine |
| Citation validity (supported claims / claims, mean per proposal) | 100% | measured |
| Repairs that fixed the first reply | 1 of 1 repaired | measured |
| Latency p50 / p95 per request (s) | 6.11 / 22.42 | measured, end to end |
| Model calls per request (mean) | 1.1 | measured (usage ledger) |

## Per item

| Item | Kind | First try valid | Repaired | Outcome | First reply's problems |
|---|---|---|---|---|---|
| edit-rewrite | edit | yes | no | published | - |
| edit-range | edit | yes | no | published | - |
| edit-insert-from-release | edit | yes | no | published | - |
| edit-insert-table | edit | yes | no | published | - |
| edit-delete | edit | yes | no | published | - |
| edit-end-summary | edit | no | yes | published | wrong_operation_count |
| edit-diagram | edit | yes | no | published | - |
| edit-two-operations | edit | yes | no | published | - |
| edit-wrong-label-repaired | edit | yes | no | published | - |
| edit-wrong-range-repaired | edit | yes | no | published | - |
| edit-hostile-diagram-repaired | edit | yes | no | published | - |
| edit-not-json-repaired | edit | yes | no | published | - |
| edit-never-valid | edit | yes | no | published | - |
| draft-proposal | draft | yes | no | published | - |
| draft-with-diagram | draft | yes | no | published | - |
| draft-two-sources | draft | yes | no | published | - |

## A sample of previews

**edit-rewrite**: Rewrite this paragraph more clearly.
  replace_block_text: "The launch is scheduled for Friday at noon." -> "The launch is scheduled for noon on Friday."

**edit-range**: Change only the word Friday to Thursday.
  replace_range: "The launch is scheduled for Friday at noon." -> "The launch is scheduled for Thursday at noon."

**edit-insert-from-release**: Add a build and deploy section using the release notes.
  insert_blocks_after: "(nothing)" -> "Build and deploy / StageOwnerBuildAliceDeployBob / Build: compile the sources and run the unit tests.Deploy: restart the workers one at a time."

**edit-insert-table**: Add a table of depot parcels from the depot report.
  insert_blocks_after: "(nothing)" -> "DepotParcels in MayBerlin4,200Lisbon3,100Madrid5,100Oslo1,900Paris6,100Prague3,600Rome4,700Vienna2,800"

## How to read this

* "Schema-valid" means the reply became typed operations (or a draft) that the shared document engine accepted against the captured document: labels, scope, anchors, ranges, links, diagrams, limits and the full-document schema check. Without repair = the first reply; with one repair = the first or the reply to one repair request.
* The stub items with a `defect` get a bad first reply on purpose, to show the repair path; the stub result is therefore a test of the harness and of the repair loop, not of a model.
* Small gold set (16 items): treat differences of a few points as noise.
