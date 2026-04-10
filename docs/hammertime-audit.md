# hammerTime Audit — Backport Analysis

**Date:** 2026-04-09  
**Auditor:** Opus (Claude Code subagent)  
**Source repo:** https://skgit.skstack01.douno.it/smilinTux/hammerTime  
**Target repos:** skmemory, skcapstone

---

## File Tree Summary

Top-level structure (code-relevant):

```
scripts/              # All Python pipeline scripts (~55 files)
  context_bridge_lib.py   # 2,800+ line unified retrieval library (THE core)
  context-bridge.py       # thin wrapper → main()
  issue-pack.py           # thin wrapper → main(mode_override="issue-pack")
  decompose.py            # L3 decomposition engine
  pipeline.py             # 5-layer orchestrator
  import-qdrant-v2.py     # L4 vector embed + Qdrant upsert
  import-falkordb-v2.py   # L5 graph import
  corpus_release.py       # corpus release/alias/promotion model
  distributed-worker.py   # multi-GPU SSH coordinator
  rebuild-stores.py       # full/incremental corpus rebuild
  research-synthesis.py   # synthesis + filing-ready packet builder
  corpus-guardian.py      # health validation daemon
  manage-decomposed.py    # decomposed artifact state manager
  case_facts.py           # case-fact loader (incident/problem/profile)
  ...47 other scripts
.cursor/skills/       # 37+ skill definitions (trigger-mapped)
json/decomposed/      # L3 output: slug.json per artifact
json/state/           # corpus release state machine files
json/releases/        # release manifests (dev/uat/prod)
json/schemas/         # JSON schemas
incidents/            # Case workspace (problem → incident → artifacts)
knowledge/            # Primary corpus (markdown)
templates/            # Filing templates
profiles/             # Person/company YAML profiles
models/bge-legal-v1/  # Sovereign embedding model (local)
```

Key infrastructure:
- **Qdrant** at `skvector.nativeassetmanagement.com` (collection: `hammertime-v3`, 1024-dim cosine, `bge-legal-v1`)
- **FalkorDB** at `skgraph.nativeassetmanagement.com:6381` (graph: `hammertime-v2`)
- **7-node GPU cluster** (chiap01–chiap08 + chiwk12) running Ollama with bge-legal-v1
- **Embedding model**: `chefboyrave21/bge-legal-v1` (1024-dim, sovereign HuggingFace model)

---

## Key Patterns Found

### L3 Decomposition

**Script:** `scripts/decompose.py` (standalone) + `skmemory.decompose.decompose_content` (shared library)

hammerTime's decomposition is domain-specialized for legal documents but structurally similar to skmemory's `decompose_content`. Key differences:

**hammerTime extras skmemory lacks:**
1. **6 citation regex types**: UCC sections, UCC forms (UCC-1/3), CFR, USC, case citations (Smith v. Jones, 123 F.2d 456), Public Law, IRS forms — skmemory has a simpler generic citation extractor
2. **Trust type extraction**: 9 named trust forms (Express, Constructive, Resulting, Statutory, etc.)
3. **Maritime/Admiralty extraction**: dedicated regex for maritime lien patterns
4. **Legal principle extraction**: 25 named principles (Holder in Due Course, Quantum Meruit, etc.)
5. **Agency/court extraction**: 20+ named agencies (IRS, Fed Reserve, CFPB, OCC, etc.)
6. **Claim confidence scoring**: chunks marked `high/medium/low` based on claim indicator phrases
7. **Section-title tracking per chunk**: exact heading ancestry embedded in each chunk
8. **CHUNK_TARGET = 900 chars / 450 tokens** (calibrated for bge-legal-v1's 512-token hard limit)
9. **Relationship extraction**: 6 rel types: CITES, CONTRADICTS, SUPERSEDES, REQUIRES, DEFINES, ESTABLISHES
10. **`secondary` / `form` document-type classification** via title term matching

**Output schema** (`json/decomposed/{slug}.json`):
```json
{
  "source_file": "knowledge/irs/example.md",
  "decomposed_at": "2026-04-05T14:21:09Z",
  "frontmatter": {},
  "stats": { "chunks": 2, "claims": 12, "citations": 4, "entities": 11, "relationships": 54 },
  "chunks": [{ "chunk_id": "...", "parent_doc": "...", "chunk_index": 0, "total_chunks": 2,
               "section_title": "...", "text": "..." }],
  "claims": [{ "claim_id": "...", "text": "...", "line": 15, "category": "irs", "confidence": "high" }],
  "citations": [{ "citation_id": "...", "raw_text": "UCC 3-301", "parsed_type": "ucc",
                  "section": "3-301", "source_file": "...", "line": 22 }],
  "entities": [{ "entity_id": "...", "name": "IRS", "type": "Agency", "context": "..." }],
  "relationships": [{ "relationship_id": "...", "source_entity": "...",
                      "relationship_type": "DEFINES", "target_entity": "...", "evidence_text": "..." }]
}
```

**Comparison to skmemory:** skmemory's `DecompositionResult` has chunks/citations/entities/claims but lacks: typed citation parsing, relationship extraction, confidence scoring, and entity-type taxonomy.

---

### Context Bridge (context_bridge_lib.py)

This is the most important file for backporting. It is a ~2,800-line retrieval fusion library that merges skmemory + hammertime corpus into a single ranked result set.

**Architecture of `build_context()`:**
1. **Pivot extraction** — calls `skmemory.decompose.decompose_content(task)` to extract entities, citations, claims from the user's query
2. **Memory loading** — `_load_memory_context()` runs: `store.load_context()`, `store.search()`, `store.novelty_search()`, `store.build_session_brief()`, and memory graph pivots (`store.graph.search_by_entity()`, `store.graph.search_by_citation()`, `store.graph.related_claims_by_entity()`)
3. **Corpus search** — `_search_corpus()` runs query embedding → Qdrant → result grouping by parent doc
4. **Jurisdiction overlay** — `_jurisdiction_overlay_hits()` does keyword-based scoring against a local pre-built index of all decomposed JSON (no live Qdrant needed)
5. **Suggestions** — `_derive_connections()` counts entity/citation/claim co-occurrence across corpus hits to surface emergent pivots
6. **Ranking** — `_build_ranked_candidates()` computes hybrid score:
   ```
   hybrid_score = base_score
                + authority_weight(tier) × 0.5
                + state_boost (0–0.22)
                + domain_boost (−0.18 to +0.28)
                + quality_boost (−0.18 to +0.28)
                + pivot_count × 0.04 (capped at 0.18)
                − 0.24 if weak_authority_reason
   ```
7. **Contradiction detection** — `_detect_contradictions()` checks top-10 results for claim conflicts (negation term asymmetry + shared citations/entities) — emits `contradiction_type`, `severity`, `shared_citations`
8. **Contradiction penalty** — weak-authority items involved in contradictions get −0.08 × count penalty
9. **Dedup** — by `(source_type, origin_path)` key

**Three research modes:**
- `balanced` — default, filters generic secondary material unless state/pivot hits
- `primary-authority-first` — aggressively suppresses generic secondary if `<2 practical terms`
- `allow-secondary` — keeps everything (AmJur/VSOF style)

**Caching layers:**
1. Query embedding cache (SHA256 key, JSON file, LRU 256 entries)
2. Corpus result cache (SHA256 key including collection+URL+state, JSON file, configurable TTL, LRU 128 entries)
3. Jurisdiction overlay index (snapshot-hash-based invalidation, JSON file)
4. Session-level Qdrant client cache (dict keyed on URL+key+timeout)

---

### Issue Pack / Authority Ranking

`issue-pack.py` calls `main(mode_override="issue-pack")` which adds extra sections beyond `build_context()`:

1. **`filing_ready` section** — infers filing type from task terms (claim_of_exemption, motion_to_vacate, objection_or_hearing_request, notice_or_affidavit), builds draft skeletons with:
   - Section prompts per filing type
   - Evidence checklist
   - `draft_markdown` — ready-to-edit filing scaffold
   - `timeline_checkpoints` — deadline-sensitive procedural steps
   - `confidence` score per skeleton
2. **`reference_bank`** — separate bucket for secondary-tier results, surfaced without displacing primary authorities
3. **`fact_gaps`** — structured questions across 4 profiles: enforcement_instrument, timing_and_service, asset_and_funds_profile, attack_paths — cross-checked against ranked results
4. **`procedural_timeline`** — inferred checkpoints (judgment posture, enforcement trigger, objection window, attack path) with high/medium urgency
5. **Draft bundle writing** — full packet materialized to disk: `preferred-draft.md`, `EXHIBIT-INDEX.md`, `SUBMISSION-CHECKLIST.md`, `FILING-PLAN.md`, `SERVICE-PACKET.md`, `HEARING-PACKET.md`, `CALENDAR.md`, `PREFERRED-PATH.md`, `NEXT-ACTIONS.md`, `packet-progress.json`

**Key struct for skmemory backport consideration:** the `reference_bank` pattern — separating primary action authorities from secondary reference material. skmemory's `search()` currently returns a flat ranked list with no tier-aware split.

---

### Graph Edge Model (FalkorDB via import-falkordb-v2.py)

**Node labels (15):**
```
Template, Process, CaseStrategy, Knowledge, Skill, Profile, Entity, Case, 
TemplateChain, Phase, Step, Incident, Judge,
[Lumina additions:] Statute, Principle, Filing, Court, Agency
```

**Relationship types (18):**
```
[v1:] HAS_PHASE, HAS_STEP, USES_TEMPLATE, REFERENCES, RELATED_TO, INCLUDES, PRESIDED_BY, CHAIN_LINK, TAGGED
[Lumina additions:] CITES, CONTRADICTS, SUPERSEDES, REQUIRES, DEFINES, ESTABLISHES, EFFECTIVE_DATE, AMENDED_BY, REPEALED_BY
```

**Entity type mapping** (from decomposed JSON → graph label):
```python
ENTITY_TYPE_MAP = {
    "statute": "Statute", "law": "Statute", "usc": "Statute", "cfr": "Statute",
    "principle": "Principle", "filing": "Filing", "court": "Court", "agency": "Agency",
    "process": "Process", "template": "Template", "knowledge": "Knowledge",
    "entity": "Entity", "person": "Profile", "company": "Entity", "skill": "Skill",
}
```

**Unique key per label** (for MERGE deduplication):
```python
{ "Statute": "name", "Principle": "name", "Filing": "filing_id", "Court": "name",
  "Agency": "name", "Memory": "memory_id", ... }
```

**Cross-reference pass:** after node import, scans skills/knowledge files for path references to build edges. Also scans all knowledge/*.md for USC/CFR citation patterns → auto-creates Statute nodes + CITES edges.

**skmemory comparison:** skmemory has `graph_queries.py` but currently only stores Memory nodes, not typed Statute/Principle/Court/Agency nodes. The CITES/CONTRADICTS/SUPERSEDES/DEFINES relationship vocabulary is entirely absent from skmemory's graph layer.

---

### Services/Workers

**`distributed-worker.py`** — Multi-GPU SSH coordinator:
- Reads `gpu-inventory.json` (7 nodes, VRAM weights: 16GB RTX 4080, 8GB RX 7600 ×2, 6GB RTX 3060, 8GB RTX 2080S, 12GB RTX 5070 Ti, 4GB Radeon 780M)
- Splits work in proportion to VRAM capacity
- SSHes into remote nodes, runs `worker-embed.py` per batch
- All nodes share NFS at `/mnt/cloud/onedrive/` — no file transfer needed
- Results upsert directly to Qdrant from each node
- Coordinator runs FalkorDB graph import centrally after embedding completes

**`corpus-guardian.py`** — Health validation daemon:
- `validate-env --target uat --deep` — validates release manifest, collection health, graph health, source drift
- `check-envs --targets dev,uat,prod --deep` — validates all env aliases in one pass
- Installed via `install-corpus-automation.sh` as systemd user timers

**`corpus_release.py`** — Release/alias state machine:
- Three-tier: `dev` (mutable), `uat` (frozen candidate), `prod` (live)
- `active_runtime()` → resolves current vector_collection + graph_name from runtime aliases
- `promote_release()` → updates alias to point a target (dev/uat/prod) at a specific release
- Processing state tracking: SHA256 content hashes, mtime, per-document last_release_id
- `diff_source_index()` → detects new/changed/deleted docs since last state snapshot

**`cron/hammertime-corpus.crontab.example`** — Suggested crontab for incremental corpus updates

---

### Skills (37+ defined)

All skills are `.md` files in `.cursor/skills/` — trigger phrases map to skill invocations. Key skills:

| Skill | Purpose |
|-------|---------|
| `sovereign-ingestion-pipeline.md` | Master SIP reference — all 5 layers, commands, schemas |
| `ingest-files.md` | L2 — convert raw docs to markdown + JSON artifacts |
| `ingest-guide.md` | Ingest a complete guide document |
| `ingest-telegram.md` | Import Telegram chat exports |
| `research-query.md` | Run context-bridge / issue-pack queries |
| `analyze-document.md` | Single-document analysis |
| `generate-document.md` | Legal document generation from templates |
| `manage-incidents.md` | ITIL-style incident lifecycle for legal cases |
| `manage-correspondence.md` | Track filings/letters/responses |
| `sync-knowledge.md` | Sync knowledge base across devices |
| `study-archive.md` | Archive and index study materials |
| `docx-to-md/` | DOCX → markdown conversion (with script) |
| `pdf-to-image/` | PDF → page images (with script) |
| `pptx-to-md/` | PPTX → markdown conversion (with script) |

**skcapstone relevance:** The skill system maps closely to skcapstone's own skills directory at `~/clawd/skills/`. The trigger-phrase-to-skill mapping and the SKILL.md metadata pattern are worth adopting for skill discoverability.

---

## Recommended Backports (Prioritized)

### 1. HIGH: Jurisdiction/Context Overlay Index → skmemory

**What:** `_build_overlay_index()` and `_jurisdiction_overlay_hits()` in `context_bridge_lib.py`

**What it does:** Builds a local JSON index from all decomposed artifacts containing each artifact's claims, citations, entities, sections, and a pre-built `search_blob`. This enables sub-millisecond keyword/state-based retrieval WITHOUT a live vector query — pure JSON scan. Falls back to this when Qdrant is slow/offline, and merges results with live semantic search.

**Why it matters for skmemory:** skmemory has no equivalent offline search. All retrieval hits skmemory's store (SQLite or vector). An overlay index of memory decompositions would let agents get fast, deterministic hits on known high-value memories before the expensive embedding path.

**Implementation path:**
- Add `build_overlay_index()` to `skmemory/store.py` or a new `skmemory/overlay.py`
- Cache at `~/.skcapstone/agents/{agent}/memory/overlay-index.json`
- Invalidate when memory file count or newest mtime changes (current snapshot pattern)
- Expose via `skmemory search --overlay` or as a fast pre-filter in `MemoryStore.search()`

---

### 2. HIGH: Hybrid Scoring with Authority Weights → skmemory + context_bridge_lib port

**What:** `_build_ranked_candidates()` + `_quality_adjustment()` + `_detect_contradictions()` in `context_bridge_lib.py`

**What it does:** Computes a `hybrid_score` that merges semantic similarity with: authority tier weight, state-specificity boost, domain relevance boost, pivot alignment bonus, and weak-authority penalty. Detects contradictions between top results and applies penalties.

**skmemory current state:** `authority_weight()` and `infer_authority_tier()` exist in `skmemory/retrieval.py` but are only used for metadata preparation — they do NOT feed back into ranking scores in `MemoryStore.search()`.

**Why it matters:** skmemory's search returns results ranked purely by semantic similarity. hammerTime's hybrid scoring demonstrably promotes actionable primary-authority results and demotes speculative secondary material. This is directly applicable to non-legal memory contexts: skmemory memories already have authority_tier metadata — it just isn't used for ranking.

**Implementation path:**
- Add `hybrid_score()` function to `skmemory/retrieval.py`
- Accept: `base_score`, `authority_tier`, `pivot_matches` (entity/citation hits in query), `domain_terms`
- Apply in `MemoryStore.search()` as a post-processing re-rank step
- Add `_detect_contradictions()` equivalent for flagging conflicting memory results

---

### 3. HIGH: Corpus Release / Processing State Machine → skcapstone

**What:** `corpus_release.py` — the full dev/uat/prod release lifecycle for vector+graph stores

**What it does:** Tracks per-document SHA256 hashes and mtimes, diffs against previous state, manages runtime aliases (which Qdrant collection + FalkorDB graph are currently "live" per environment), writes release manifests, validates health, and allows promoting releases between tiers without rebuilding.

**Why it matters for skcapstone:** skmemory has no concept of corpus release state. Every rebuild wipes and recreates. When `hammertime-v3` grows to thousands of points, incremental state tracking becomes critical. skcapstone also manages multi-agent deployments where different agents may need different corpus versions (dev/uat/prod).

**Implementation path:**
- Port `corpus_release.py` as `skcapstone/src/skcapstone/corpus_release.py`
- Integrate with `skmemory` so each agent's Qdrant collection has an aliased runtime name
- Add `skcapstone corpus release` and `skcapstone corpus promote` CLI commands
- Store release state at `~/.skcapstone/corpus/{agent}/state/`

---

### 4. MEDIUM: Typed Citation Extraction → skmemory/decompose.py

**What:** The 9 citation regex types in `decompose.py` (UCC, CFR, USC, case citations, Public Law, IRS forms, trust types, maritime, legal principles)

**Why it matters:** skmemory's `_extract_citations()` uses simpler patterns. If SK agents are going to use hammertime-v3 in recall_collections, the same citation normalization should be shared so citations extracted during memory storage match citations extracted during corpus query pivot extraction.

**Implementation path:**
- Extract citation regexes into `skmemory/citation_patterns.py`
- Update `skmemory/decompose.py` `_extract_citations()` to use shared patterns
- hammerTime already calls `skmemory.decompose.decompose_content()` for pivot extraction — the shared library already exists, it just needs richer patterns

---

### 5. MEDIUM: Query Expansion + Stale Cache Fallback → skmemory

**What:** `_expand_query()` (domain-aware query expansion) + stale cache fallback in `_search_corpus()`

**What it does:** When a query contains enforcement/exemption terms, appends related terms ("writ of execution", "judgment debtor", etc.) to the embedding query. When Qdrant fails mid-query, falls back to last cached result for that exact query (keyed by SHA256 of query+collection+state).

**Why it matters:** skmemory has no query expansion and no graceful degradation when the vector backend is unavailable. Both patterns would improve skmemory's recall and reliability.

---

### 6. MEDIUM: Graph Relationship Vocabulary → skmemory/graph_queries.py

**What:** CITES, CONTRADICTS, SUPERSEDES, REQUIRES, DEFINES, ESTABLISHES, EFFECTIVE_DATE, AMENDED_BY, REPEALED_BY relationships in FalkorDB

**Why it matters:** skmemory's graph layer currently uses RELATED_TO as the primary edge type. Adding typed edges would enable structured queries like "which memories CONTRADICT this one" or "which memories SUPERSEDE that prior belief" — directly useful for emotional continuity (FEBs that supersede old beliefs) and knowledge evolution tracking.

**Implementation path:**
- Add typed relationship support to `skmemory/graph_queries.py`
- Expose `store.graph.search_related_claims_by_type(entity, rel_type)` 
- Use relationship type when saving FEB memories that explicitly contradict or update prior beliefs

---

### 7. MEDIUM: Draft Bundle / Packet Pattern → skcapstone coordination

**What:** The filing-ready draft bundle: preferred-draft, exhibit index, submission checklist, filing plan, service/hearing packet, calendar, preferred-path, next-actions, packet-progress.json

**Why it matters:** This is structurally equivalent to skcapstone's coordination task/project pattern. The hammerTime "packet" is a self-contained action workspace that survives across sessions. skcapstone's `~/.skcapstone/coordination/tasks/` does something similar but less structured. The packet-progress.json with status fields (preferred_draft_status, service_status, hearing_status, filing_status) maps directly to task state management.

---

### 8. LOW: Distributed Worker Pattern → skcapstone swarm

**What:** `distributed-worker.py` — VRAM-weighted work distribution across SSH nodes

**Why it matters:** When skmemory or hammerTime corpus needs to be re-embedded across a large document set, the 7-GPU cluster pattern (VRAM-proportional batching, SSH dispatch, NFS shared storage) could be reused. The `gpu-inventory.json` file already exists as shared infrastructure.

**Implementation path:** Low priority because skmemory embeddings are per-memory-file (small), not bulk corpus runs. Relevant only if skmemory gains a sovereign embedding model.

---

### 9. LOW: Corpus Guardian / Health Daemon → skcapstone monitoring

**What:** `corpus-guardian.py` validates collection health, graph health, source drift, and alias correctness

**Why it matters:** skcapstone has no health monitoring for Qdrant or FalkorDB. If these services are used by multiple agents, a guardian daemon would catch drift (source changed but corpus not rebuilt) and surface it proactively.

---

## recall_collections Recommendation

`hammertime-v3` (Qdrant at `skvector.nativeassetmanagement.com`, 1024-dim, bge-legal-v1) should be added to `recall_collections` for:

| Agent | Justification |
|-------|---------------|
| **lumina** | Primary orchestrator — needs legal corpus access for case-related dispatches |
| **architect** | Handles system design that may intersect legal/compliance domains |
| **scholar** | Research-focused agent — direct benefit from legal corpus retrieval |
| **coder** | Lower priority but useful for regulatory/compliance code generation |

**Configuration note:** The bge-legal-v1 model is 1024-dim while the default skmemory collection uses a different embedding model. Recall from `hammertime-v3` requires either:
1. Using the same bge-legal-v1 model for query embedding (requires the local model at `models/bge-legal-v1/` or via Ollama), OR
2. Adding a separate recall path in `context_bridge_lib.py` that hammerTime already implements

The cleanest path: add `hammertime-v3` as a read-only recall collection that only `context-bridge.py` / `issue-pack.py` queries, surfacing results back into skmemory's context system through the bridge interface that already exists in `context_bridge_lib.py`.

**Do NOT** route skmemory's standard search to hammertime-v3 directly — the embedding dimensions don't match skmemory's default model.

---

## Dependencies hammerTime Uses That skmemory/skcapstone Should Adopt

| Library | hammerTime Use | skcapstone/skmemory Status | Recommendation |
|---------|---------------|--------------------------|----------------|
| `sentence-transformers` | L4 embedding (bge-legal-v1 via SentenceTransformer) | Used in skmemory | Already adopted |
| `qdrant-client` | Vector store (hammertime-v3) | Used in skmemory | Already adopted |
| `redis` (FalkorDB) | Graph DB via GRAPH.QUERY commands | Not in skmemory | Add for graph layer if FalkorDB adopted |
| `falkordb` | Newer FalkorDB Python client | Not in skmemory | Optional — redis path works |
| `concurrent.futures` | ThreadPoolExecutor for Qdrant query timeout | Not systematically used | Add for retrieval deadline enforcement |
| `yaml` | YAML frontmatter in profiles | Already in skcapstone | Already adopted |

**No new mandatory dependencies** are required for the highest-priority backports (overlay index, hybrid scoring). They use only stdlib + existing skmemory internals.

---

## Architecture Note: What hammerTime Does Differently From skmemory

| Concern | skmemory | hammerTime |
|---------|----------|------------|
| Storage unit | `Memory` object (agent-personal) | Document chunk (corpus-shared) |
| Embedding dimensions | Variable (default model) | 1024 (bge-legal-v1 sovereign) |
| Graph node | Generic `Memory` node | 15 typed nodes (Statute, Court, Agency, etc.) |
| Graph relationships | RELATED_TO primarily | 18 typed rels including CITES, CONTRADICTS, SUPERSEDES |
| Retrieval | Semantic similarity only | Hybrid: semantic + overlay + authority weight + state boost |
| Authority ranking | Metadata only (not used in ranking) | Fully integrated into hybrid_score |
| Contradiction detection | None | Top-10 cross-check + penalty |
| Release lifecycle | None (rebuild = wipe) | dev/uat/prod alias model with SHA256 drift tracking |
| Query caching | None | Two-level: embedding LRU + corpus result TTL + stale fallback |
| Offline mode | None | Jurisdiction overlay (pure JSON, no live vector needed) |

The two systems are **complementary, not redundant**. hammerTime is the document corpus for legal knowledge; skmemory is the agent's personal experiential memory. The `context_bridge_lib.py` is the correct integration point — it already merges both. The backport work is about lifting hammerTime's superior retrieval patterns (hybrid scoring, caching, overlay index) into skmemory so the memory side is equally capable when the bridge merges results.
