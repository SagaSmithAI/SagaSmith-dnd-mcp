# Strict Parsing Convergence Audit — 2026-08-09

## Outcome

The public authoring surface is exactly three facades:

- `rulebook_draft(start/get/evidence/edit/finalize)`;
- `module_draft(start/get/evidence/edit/finalize)`;
- `content_pack(list/get/test/build/import/export/install/activate/deactivate/remove)`.

The seven former import/review/Pack facades are not registered and have no
compatibility aliases. Core and D&D perform the mechanical pass; Agent decisions
remain revisioned with the editable draft and Skill, and only a completed draft
may be finalized.

The strict handover target is complete for the current parser implementation.
The earlier version of this report incorrectly treated retirement of the first
five high-risk heuristics as completion of the whole boundary audit. This
revision covers the remaining source-order, proximity, fuzzy-identity, and
named-entry repairs as well.

The result deliberately does not maximize automatic extraction. Ambiguous
content remains pending, catalog-only, or Agent-review-required. No content
pack was rebuilt or published.

## Enforced ownership boundary

- `sagasmith-core` owns system-neutral character cleanup, page/source spans,
  geometry, repeated margins, visual headings, positioned-text reflow, and
  evidence-scored OCR fallback.
- `sagasmith-dnd` owns edition-pinned vocabulary and formal D&D field grammar.
- Book-, page-, layout-, or entry-specific corrections belong to replayable MCP
  source review with exact selectors.
- Semantic identity or ownership without sufficient source evidence remains
  unresolved for Agent review.

Runtime parser files contain no rulebook names or named-entry correction. The
2014 standard subclass identities used by classification now live in the
versioned `parsing_vocabulary.py` rule pack rather than the parser body.

## Retired automatic heuristics

The registry now retains fourteen rejected legacy rules. The original five
remain disabled:

1. split identical heading paths by physical occurrence;
2. merge/split same-name features by source-chunk overlap;
3. bind a feature to the nearest preceding subclass;
4. infer a dependent statblock owner from neighboring candidates;
5. reject a background solely because its title is generic.

Nine additional refinements were removed in the strict pass:

1. join adjacent feat heading fragments and `OPTIONS` continuations;
2. build class boundaries from ordered `Class Features` anchors and fixed
   lookback windows;
3. build species/subrace boundaries from order and a `Languages` stop marker;
4. merge an adjacent feature into an item by suffix or running chapter header;
5. merge subclass spell-table identities by one-edit similarity;
6. assign spell-list classes by column order and level resets;
7. infer an unnamed statblock identity from the preceding twenty chunks;
8. infer class ownership from any class mention in a prose window;
9. repair punctuation by matching the named `Ignited Illumination` entry.

An unnamed but structurally complete statblock is now retained as a low-
confidence review candidate. It is not marked `review_ready` until the source
itself evidences its identity.

## Active rule registry and executable gate

The registry contains 31 entries:

| Category | Count | State |
|---|---:|---|
| `document_invariant` | 7 | active |
| `dnd_grammar` | 8 | active |
| `ruleset_vocabulary` | 1 | active |
| `source_review` | 1 | active |
| `legacy_candidate` | 14 | rejected |

Every entry records owner, evidence, affected formats, counterexample,
confidence, fallback, tests, and provenance. Major executable parser rules are
decorated with `registered_parsing_rule`; importing an unknown or rejected rule
fails immediately. AST/source boundary tests additionally reject the retired
functions, fixed proximity windows, rulebook markers, and named-entry repairs.

## Editable Pack and Agent Skill convergence

The post-extraction lifecycle is now deliberately simpler:

1. Core normalizes the document and retains exact source/page/chunk evidence.
2. D&D mechanically extracts candidates and applies only deterministic D&D
   normalization.
3. `rulebook_draft(edit)` lets the Agent repeatedly include, exclude, reopen, or
   replace source-bound candidate artifacts. Each edit reruns evidence and D&D
   validation and persists issues, fingerprints, editor, operation, note, and
   candidate edit history with the import job.
4. Accepted/rejected are editable dispositions, not a frozen review state.
   `rulebook_draft(finalize)` is the only transition to `reviewed`; it requires the
   latest revision, a complete disposition set, zero deterministic blockers,
   valid source-bound contracts, and an explicit completion note.
5. Finalization compiles and saves the immutable Pack atomically. Pack testing,
   installation, and activation remain separate `content_pack` operations.

The mandatory primary/critic state-machine gate was removed from draft
authoring. A critic remains an optional review method, but one source-bound
Agent edit can be finalized after deterministic checks. Book-specific decisions
stay with the editing Pack/import job; the shared D&D Skill owns only the Agent
workflow and permissions in `references/parsing-agent-edit-loop.md`.

Split and merge remain explicit, auditable authoring operations: add the
source-bound reviewed result and reject the superseded candidates. They do not
reactivate proximity, order, or fuzzy-identity parser heuristics.

## Spell ownership and performance correction

Spell ownership is retained only when the current list heading uniquely names
a class or an explicit source declaration states membership in named spell
lists. Fused multi-class columns are no longer assigned by printed order or
level reset.

Strict parsing exposed a performance defect: an intentionally empty class
index was treated as missing through `class_index or ...`, causing a full-book
rescan for every PHB spell. The parser now distinguishes `None` from an empty
index, computes explicit declaration ownership once, and prefilters chunks by
spell heading/schema evidence. PHB returned from more than fourteen minutes
without completion to a completed 35.789-second candidate extraction.

## Heterogeneous frozen baseline

Mode: `--baseline-only --no-ocr`, isolated MCP homes, no Agent review,
compilation, export, round trip, or content-package output.

| Source | Entities | Unresolved | Duplicates | Coverage | Extract s | Pipeline s |
|---|---:|---:|---:|---:|---:|---:|
| DMG | 643 | 643 | 27 | 1.000 | 0.102 | 0.303 |
| Eberron: Rising | 319 | 319 | 13 | 1.000 | 0.062 | 0.167 |
| Monster Manual | 571 | 571 | 24 | 1.000 | 0.146 | 0.286 |
| Mordenkainen | 281 | 281 | 7 | 1.000 | 0.088 | 0.299 |
| PHB | 936 | 936 | 44 | 1.000 | 35.789 | 35.969 |
| Tasha | 498 | 498 | 19 | 1.000 | 4.861 | 14.771 |
| Volo | 237 | 237 | 6 | 1.000 | 5.103 | 24.293 |
| Xanathar | 482 | 482 | 10 | 1.000 | 8.890 | 20.688 |
| Lost Mine of Phandelver | 128 | 128 | 6 | 1.000 | 1.376 | 5.656 |
| **Total** | **4,095** | **4,095** | **156** | **1.000** | **56.417** | **102.432** |

All 4,095 candidates remain pending/unresolved in baseline-only mode. Fragment
count is zero, every candidate retains source chunks, and no review result was
auto-promoted. Increased candidate counts are retained review surface, not
accepted structured content and not a refreshed expected-data contract.

Frozen reports:

- `.test-tmp/parsing-strict-books-20260809/books-baseline.json`
- `.test-tmp/parsing-strict-lmop-20260809/lmop-baseline.json`

## Verification

- `sagasmith-core`: full test suite passed, with its existing optional skip.
- `sagasmith-dnd`: full test suite passed after strict-boundary and performance
  regressions were added.
- Parser-focused `content_import`, `statblocks`, vocabulary, registry, and
  strict-boundary tests passed.
- MCP rulebook regression-driver tests passed (68 tests).
- Ruff passed for every touched parsing, vocabulary, registry, boundary, and
  regression-driver file.
- Eight heterogeneous rulebooks and one campaign module passed the frozen raw
  baseline.
- The updated Core full suite and D&D full suite passed.
- MCP import lifecycle, facade/capability, Skill plan, and 68-rulebook-driver
  suites passed, including finalization replay and compile-before-finalize
  rejection.
- The full MCP suite passed after its expectations were aligned with the
  current SRD pack version and Monk armor-effect contract.
- Ruff passed on every file changed for this lifecycle. The updated D&D Skill
  passed `quick_validate.py` and the real Skill Plan budget tests.

## Remaining review boundary

The following are not parser failures and must not trigger new general rules:

- source text whose identity exists only in neighboring prose;
- flattened class, species, feat, item, or spell-list ownership without an
  explicit source boundary;
- one-edit OCR identities between distinct cards;
- individual punctuation or field corrections such as the removed named-entry
  repair;
- ambiguous dependent actor ownership;
- malformed visual statblocks requiring OCR or Agent review.

Future promotion requires three distinct source layouts, one explicit
counterexample, unchanged unrelated counts, non-decreasing source coverage, no
silent drop, no review auto-promotion, and deterministic replay. A single-book
failure remains fixture/review work.

## Execution constraints

- Existing unrelated working-tree changes were preserved.
- `standalone/` was not changed.
- Original documents and source content were not rewritten.
- No content pack was rebuilt or published.
- No Git commit was created because the touched parser files already contained
  substantial pre-existing user changes.
