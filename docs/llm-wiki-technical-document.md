# llmwiki — Technical Document

**Audience:** an engineer who needs to support, extend, fix, or test this
repository, without having read the design doc or implementation plan first.
**Scope:** the codebase as it exists on disk today (Phase 0, plus plan-v1.4
§19's R1–R5: multi-provider LLM routing and query-agent skill invocation).
This document describes *implemented behaviour*. Where the roadmap differs
from today's code, that is called out explicitly rather than blended in.

**Primary sources this document distills**, and where to go for more:

| Document | What it's for |
|---|---|
| `docs/llmwiki-KB-design_v1.4.md` | Why the system is shaped this way — architecture rationale, phase plan. Read before making a *design* decision. |
| `docs/implement-plan-v1.4.md` | The packaging/extraction plan (mostly not yet executed — see §9). |
| `docs/HISTORY.md` | Chronological log of every change, bug, and deviation. The ground truth for "why is this line like this". |
| **This document** | The map: which class calls which, how to extend each seam, and the full API surface. Optimized for "I need to change X" and "what does Y expose". |

---

## 1. System Overview

llmwiki is a **research knowledge base**: sources you capture (PDF files,
blog/article URLs, YouTube videos, pasted text and text files — §3.1.1) are
stored immutably, then an LLM
incrementally compiles them into an interlinked markdown wiki. A query agent
answers questions from that wiki first, falling back to raw vector search,
and every citation it returns is verified to resolve back to a real captured
source. The compiler is deliberately non-agentic (fixed stages, fixed
prompts); the query agent gained one narrow piece of genuine agentic
behaviour in R5 — choosing, and chaining, among a discoverable catalog of
skills before it answers (§2.4, §3.3).

```
Capture → raw/ (object storage) → Extract → Chunk + Embed → Vector Store
                                          └→ Incremental Wiki Compiler → wiki/
Query Agent reads wiki/ first, falls back to Vector Store, answers with citations.
```

Three things make the codebase tractable to work in:

1. **A strict, mechanically-enforced layer ladder** (§2) — you cannot import
   "upward" and have the test suite pass.
2. **One function surface, three transports** — `tools.py` contains the
   entire business API; the REST routes, the MCP tools, and the CLI are thin
   serialization shims over the exact same functions (§3.4, §6).
3. **Everything that touches the network is a `Protocol` with a real adapter
   and a fake** — `ObjectStore`, `VectorStore`, `Embedder`, `LLMClient`. Tests
   and `--offline` runs swap in the fakes; nothing else changes (§2.3).

---

## 2. Module Map and Layering

### 2.1 The layer ladder

`src/llmwiki/` is organized as directories, each one a "layer". Dependencies
point **inward/downward only**. This is not a convention — it is enforced by
`tests/unit/test_layering.py`, which walks the AST of every module (including
function-local imports) and fails the build on a forbidden edge. If you add
an import that violates the ladder, **move the code**, don't widen the rule
— `CLAUDE.md` names this test load-bearing.

```
L0  models/                (pure pydantic schemas, zero I/O, imports nothing else in llmwiki)
     │
L1  storage/  extractors/  embedding/  vector/  llm/     (adapters — each Protocol-based, siblings, never import each other)
     │
L1* chains/                (prompt CONTENT + a cached file loader — no domain logic; §2.4)
     │
L2  wiki/     agent/                                     (domain logic — compiler, page I/O, query agent + its skills.py; §2.4)
     │
L3  pipeline/                                             (orchestrates extract → chunk → embed → compile)
     │
L4  tools.py                                               (the entire public function surface — the "brain")
     │
L5  api/  mcp/  cli.py                                     (transports — validate input, call tools.py, serialize)
```

Two things live entirely **outside** `src/llmwiki/` and are not part of the
installed package at all: repo-root `config/` (§5.6) and repo-root `skills/`
(§2.4, §5.8). Neither is a "layer" in the ladder above — `test_layering.py`
and `test_package_boundaries.py`-style guards do not scan them.

Two allowed exceptions, both serialization not logic: `api/` and `cli.py` may
import `wiki.pages.render_page` to render a page as markdown text, and
`cli.py` imports `factory` (it is an entry point, not a domain layer). See
`ALLOWED_EXCEPTIONS` in `test_layering.py` for the exact list — do not add to
it without also adding the reason there.

`factory.py` and `config.py` sit **outside** the ladder deliberately:
`config.py` is the only module allowed to read `os.environ` (besides the
routing-config loader, §5.6), and `factory.py` is the only place concrete
adapters (`boto3`, `anthropic`, `pymupdf`, …) get imported — always
function-locally, so an offline run never even imports them. Only `tools.py`
and the entry points (`cli.py`) call `factory`; domain classes (`Compiler`,
`QueryAgent`, `IngestPipeline`) take already-built protocol instances in
their constructors and never call the factory themselves. This is what makes
them testable with a spy store or a scripted LLM.

### 2.2 Directory-by-directory

| Path | Layer | Responsibility |
|---|---|---|
| `models/` | L0 | Pydantic schemas only: `source.py`, `chunk.py`, `page.py`, `plan.py`. No I/O. |
| `storage/` | L1 | `ObjectStore` protocol (`base.py`), `LocalObjectStore`, `R2ObjectStore`, and `layout.py` (the *only* module that builds object keys). |
| `extractors/` | L1 | `Extractor` protocol + modality detection (`base.py`), and one module per modality: `pdf.py`, `web.py`, `youtube.py`, `text.py`. |
| `embedding/` | L1 | `Embedder` protocol (`base.py`), `WorkersAIEmbedder`, `FakeEmbedder`. |
| `vector/` | L1 | `VectorStore` protocol (`base.py`), `VectorizeStore`, `MemoryVectorStore`. |
| `llm/` | L1 | `LLMClient` protocol (`base.py`), `AnthropicLLM`, `LangChainLLM`, `FakeLLM`, the provider registry, pricing, and the R1–R3 multi-provider router (`router.py`, `routing_config.py`). |
| `wiki/` | L2 | `compiler.py` (the incremental compiler), `pages.py` (read/write/parse), `gists.py` (the manifest + index), `lint.py` (scheduled global check). |
| `agent/` | L2 | `query.py` — `QueryAgent`, wiki-first retrieval with RAG fallback, plus (R5) skill selection/chaining. `skills.py` — `discover_skills()`, reads the repo-root `skills/` SKILL.md catalog. See §2.4. |
| `chains/` | L1/L2-adjacent | `prompts_loader.py` + `prompts/*.md` — the five prompt templates the compiler (always) and query agent (only when no `skills/` catalog is discovered) use. **Not** the same thing as repo-root `skills/` — see §2.4. |
| `pipeline/` | L3 | `ingest.py` (`IngestPipeline` — capture/extract/embed/compile orchestration), `chunker.py` (heading-aware text chunking). |
| `tools.py` | L4 | Every function any transport calls. This *is* the public Python API (§6.1). |
| `api/` | L5 | `app.py` (FastAPI app + MCP mount), `routes.py` (HTTP handlers). |
| `mcp/` | L5 | `server.py` — the six agent-facing MCP tools, same functions as `api/routes.py`. |
| `cli.py` | L5 | `llmwiki` console script. |
| `factory.py` | outside ladder | Builds concrete adapters from `Settings`. Only `tools.py` and `cli.py` call it. |
| `config.py` | outside ladder | `Settings` (pydantic-settings) — the only module reading `.env`/`os.environ`, except `llm/routing_config.py` (§5.6). |

Repo-root `config/` (note: **not** `src/llmwiki/config.py`) is a separate,
uninstalled thing — see §5.6. Repo-root `skills/` (note: **not**
`src/llmwiki/chains/prompts/`) is the same kind of thing, for the query
agent's skill catalog — see §2.4 and §5.8.

### 2.3 The adapter pattern (why tests never touch the network)

Every external dependency is a `typing.Protocol`:

```python
# storage/base.py, extractors/base.py, embedding/base.py, vector/base.py, llm/base.py
class ObjectStore(Protocol): ...
class Extractor(Protocol): ...
class Embedder(Protocol): ...
class VectorStore(Protocol): ...
class LLMClient(Protocol): ...
```

`factory.py` maps `Settings` fields to concrete instances, with **function-local
imports of the concrete SDKs** (`boto3`, `anthropic`, `pymupdf`, `trafilatura`,
`youtube_transcript_api`) so importing `factory` itself never pulls one in.
Domain classes (`Compiler`, `QueryAgent`, `IngestPipeline`) are constructed
with already-built protocol instances — they never call `factory` themselves.
That is the seam tests exploit: pass a `MemoryVectorStore`, a
`LocalObjectStore`, a `FakeEmbedder`, a `FakeLLM` (or a scripted spy for any
of them) and you get the real control flow with zero network calls.

`--offline` (CLI flag, or `docker compose --profile offline`) sets four env
vars (`STORAGE_BACKEND=local`, `VECTOR_BACKEND=memory`,
`EMBEDDING_BACKEND=fake`, `LLM_PROVIDER=fake`) and, since the R1–R3 routing
feature landed, **also** points `LLMWIKI_PROVIDERS_CONFIG`/`LLMWIKI_OPS_CONFIG`
at a guaranteed-nonexistent path — otherwise a real `config/providers.py` in
the checkout would silently outrank `LLM_PROVIDER=fake` (see §5.6 and the
2026-09-06 `HISTORY.md` entry for the bug this fixed).

### 2.4 Agents, `chains/prompts/`, and `skills/` — three things with similar names

This codebase has exactly **two** things that ever call an LLM as part of
answering a request — the compiler and the query agent — and, as of R5, only
one of them is "agentic" in the tool-calling sense. The rest of this section
exists because `chains/` and `skills/` look interchangeable at a glance
(both are directories of markdown files with YAML frontmatter) and are not:
they are consumed by different code, at different times, for different
reasons.

**0. What each agent actually is.**

| | `Compiler` (`wiki/compiler.py`) | `QueryAgent` (`agent/query.py`) |
|---|---|---|
| Called from | `IngestPipeline.process()` → `compile_source()` (§3.1) | `tools.answer()` (§3.3) |
| Shape | Five **fixed** stages, always in the same order (§3.2) | One retrieval pass, then (R5) an optional selection step, then generation |
| LLM calls per run | Up to 4 (`summarize_source`, `plan_compile`, `create_page`\*, `patch_page`\*) | 1 (pre-R5, or no `skills/` catalog) to `1 + MAX_SKILL_CHAIN` (R5, catalog present) |
| Which prompt runs, decided by | **The Python source code.** Each stage's function body names its own `op=` and calls `load_prompt("<that literal name>")` — there is no branch, no choice, no model input into this decision | Pre-R5 / no catalog: also the source code, identically. **R5, catalog present: the model**, via a forced tool-call (§3.3) |
| Is this "agentic"? | **No, deliberately.** Design v1.4 §4.4/§4.8.2 requires the compiler to stay non-agentic — a model that could decide to re-plan, retry, or call something unexpected is exactly what would break the "compilation cost never grows with wiki size" guarantee `test_compiler_no_full_scan.py` enforces | **Only this one, only since R5, only for picking/chaining a system prompt.** It cannot decide to skip retrieval, re-query, or call any tool other than "choose a skill" — see §3.3 for the exact boundary |

\* `create_page`/`patch_page` run zero or more times, once per planned
operation (§3.2 step 4), not fixed at exactly one call.

**1. The original structure (still true for the compiler, and for the query
agent whenever no `skills/` catalog exists): `chains/prompts/*.md` is content,
not logic.**

`chains/prompts_loader.py:load_prompt(name)` is a cached file reader with one
job: given a literal string, return the body of `prompts/{name}.md`. It has
no opinion about *when* `name` should be `"plan_compile"` vs `"answer_query"`
— that decision is compiled into the Python call site itself:

```python
# wiki/compiler.py — always this literal string, every run, no exception
response = self.llm.complete(op="summarize_source",
                              system=load_prompt("summarize_source"), ...)

# agent/query.py, pre-R5 (and still today, whenever discover_skills() finds nothing)
response = self.llm.complete(op="answer_query",
                              system=load_prompt("answer_query"), ...)
```

R4 (2026-09-07) added `name`/`description` YAML frontmatter to all five files
so each is independently readable as an Agent Skill by an *external* harness
(Claude Code, the Claude Agent SDK, an MCP client) — but that changed nothing
about how *this codebase* uses them. `load_prompt()` still strips the
frontmatter and returns the body only, and every call site above is exactly
as fixed as it was before R4. **If you are adding a new compiler stage, you
are extending this fixed structure — see §5.3, not §5.8.**

**2. The new structure (R5, query agent only): `skills/*.md` is a catalog the
model chooses from, at request time.**

Repo-root `skills/` (a **different directory** from
`src/llmwiki/chains/prompts/` — see the warning in §2.2) holds the same kind
of SKILL.md-format files, but nothing in `agent/query.py` hardcodes which one
runs. Instead:

```
agent/skills.py:discover_skills(settings.agent_skills_dir)
    scans every *.md under the directory, at the START of every answer() call
    → dict[name, Skill(name, description, body, path)]     — an open set,
      not a fixed list of call sites; adding a skill = adding a file, no code change

agent/query.py:QueryAgent._select_skills(query, skills)
    shows the model the discovered {name: description} listing
    ONE forced-schema LLM call (op="answer_query", schema names the discovered
    names as an enum) → the model picks 1..MAX_SKILL_CHAIN of them, in order
    invalid/hallucinated choice → retry once → fall back to the fixed
    chains/prompts/answer_query.md skill (never a hard failure)

agent/query.py:QueryAgent._answer_with_skills(...)
    runs the chosen skill(s) in order: each is ONE more op="answer_query" call
    whose `system` is THAT skill's body (not chains/prompts/answer_query.md);
    step 2+ also receives step 1's output text appended to its prompt
```

The generation call's *shape* never changes —
`LLMClient.complete(op="answer_query", system=..., prompt=...)`, the same
signature the pre-R5 code always used. What changed is that `system` is no
longer always the literal return value of `load_prompt("answer_query")`; it
is now, when a catalog exists, whichever skill body the model picked for
*this specific question*. See §3.6 for how this fits together with *which
concrete `LLMClient` class* actually executes that call — the two decisions
(which text, which provider) are made by unrelated code and never see each
other.

| | `chains/prompts/*.md` | repo-root `skills/*.md` |
|---|---|---|
| Read by | `chains/prompts_loader.load_prompt(name)` | `agent/skills.py:discover_skills(dir)` |
| Used by | Compiler (always, all 4 stages) + query agent (only as the R5 fallback) | Query agent only (R5), when the directory has ≥1 valid file |
| Which file runs | Hardcoded per call site, in Python | Chosen by the model, per question, via a tool-call |
| Adding a new one | Requires a new `op=` value + a new call site (§5.3) | Drop a new `.md` file in `skills/` — **no code change** |
| Malformed file | N/A — `FileNotFoundError` if a hardcoded name is missing | Logged and **skipped**, not fatal (`agent/skills.py`) — the agent must keep answering even if one skill file is broken |
| Location | `src/llmwiki/chains/prompts/` — inside the installed package | Repo root `skills/` — outside `src/llmwiki/`, like `config/` (§5.6), not installed, not scanned by any layering test |
| Ships with (this repo) | `summarize_source.md`, `plan_compile.md`, `create_page.md`, `patch_page.md`, `answer_query.md` | `answer_query.md` (default, single-skill), `compare_concepts.md` |

**Gotchas worth knowing before touching either directory:**

- Every test in `tests/unit/` except `test_agent_skill_invocation.py` runs
  with `Settings.agent_skills_dir` pointed at a **guaranteed-absent**
  directory (`conftest.py`'s autouse `_isolate_agent_skills_dir` fixture). If
  you add a test that calls `QueryAgent.answer()` and expect skill selection
  to run, you must pass your own `agent_skills_dir=` — the default fixture
  will otherwise silently give you the fixed-prompt path.
- All skill-related LLM calls (the selection call and every chosen skill's
  generation call) are recorded under the single op label `"answer_query"` in
  the cost ledger — there is currently no way to tell, from
  `wiki/_meta/cost.jsonl` alone, how many of a given `answer_query` call's
  tokens were spent choosing a skill versus generating the answer. This was a
  deliberate scope decision (avoids a new `op=` value and the config/AST-guard
  churn that would come with one) — revisit if per-step cost visibility
  becomes important; `implement-plan-v1.4.md` §19.9 item 3 flags the related
  open question of LangSmith span granularity.
- `agent/skills.py` is in the `agent` layer (L2) — same `test_layering.py`
  rules as `agent/query.py` apply to it (may not import `api/mcp/cli/
  pipeline/tools/factory`).
- The compiler is **never** a consumer of `skills/` and is not expected to
  become one — see the "No, deliberately" row above. Do not wire
  `discover_skills()` into `wiki/compiler.py`.

---

## 3. End-to-End Workflows (class/file → class/file)

### 3.1 Ingest → Compile

Entry points: `POST /ingest` or `POST /upload` (`api/routes.py`), the MCP
tool `ingest_source`, or `llmwiki ingest` (`cli.py`). All three call into
`tools.py`. What they accept is one of three *inputs* covering five *source
kinds* — see §3.1.1.

```
api/routes.py:ingest()              ┐
mcp/server.py:ingest_source()       ├─► tools.py:ingest_source()  ──► IngestPipeline.capture()
cli.py (ingest command)             ┘        (or tools.ingest_now, which also runs process())

IngestPipeline.capture(url= | file= | text=)  [pipeline/ingest.py]
  ├─► IngestPipeline._source_id()                              (validate exactly one input,
  │      storage.layout.source_id_for_bytes / source_id_for_url  then content-address it)
  ├─► ObjectStore.exists(raw/{id}/meta.json)  → duplicate?     (BEFORE any fetch)
  ├─► extractors.base.detect_modality                          (pick pdf/web/youtube/text)
  ├─► extractors.web.fetch / extractors.youtube.fetch_transcript  (URL sources only, at capture time)
  ├─► extractors.base.detect_modality  (again, on the SERVED content type — see §3.1.1)
  └─► ObjectStore.put()  → raw/{id}/original.*, raw/{id}/meta.json     (immutable, written once)
  returns SourceRef{source_id, status="queued"} immediately

--- background task (api) or synchronous continuation (CLI/MCP) ---

IngestPipeline.process(source_id)  [pipeline/ingest.py]
  ├─► IngestPipeline.extract()
  │     └─► extractors.base.get_extractor(modality).extract()   → ExtractedDoc
  │           (PdfExtractor / WebExtractor / YouTubeExtractor / TextExtractor)
  │         ObjectStore.put()  → raw/{id}/extracted.md
  │
  ├─► IngestPipeline._embed()
  │     ├─► pipeline.chunker.chunk_document(doc)                → list[Chunk]
  │     ├─► Embedder.embed([chunk texts])                        → vectors
  │     └─► VectorStore.upsert(chunks index, ...)
  │
  └─► Compiler.compile_source(doc)     [wiki/compiler.py]  — see §3.2
```

### 3.1.1 The five source kinds

`capture()` takes **exactly one** of three inputs — more than one, or none,
is a `ValueError` (a 422 over REST). Those three inputs cover five source
kinds, and which kind a source *is* is always **detected, never declared by
the caller**:

| Source kind | Input | Modality | Stored as | Extractor |
|---|---|---|---|---|
| PDF file | `file=` bytes (+ `filename`/`mime`) | `pdf` | `original.pdf` | `extractors/pdf.py` — text layer per page, `### Page N` markers; a scan with no text layer fails loudly (no OCR in Phase 0) |
| Blog / article URL | `url=` | `web` | `original.html` | `extractors/web.py` — trafilatura to markdown |
| YouTube URL | `url=` | `youtube` | `original.json` (the transcript) | `extractors/youtube.py` — timestamped `### HH:MM:SS` paragraphs |
| Pure text | `text=` | `text` | `original.txt` | `extractors/text.py` — decode + normalize |
| Text file | `file=` bytes (`.txt`/`.md`) | `text` | `original.txt`/`.md` | same as above |

**How the modality is decided** (`extractors/base.py:detect_modality`, ordered):

1. **URL shape wins first.** `youtube.py:is_youtube_url()` requires *both* a
   YouTube host and an extractable 11-character video id, so `/watch`,
   `youtu.be/`, `/shorts/`, `/live/`, `/embed/` and `m.` hosts are all
   transcripts — while a channel or playlist page, having no video id, falls
   through and is captured as an ordinary web page.
2. `application/pdf`, or a `.pdf` filename → `pdf`.
3. `image/*` → `image`.
4. `text/html`, `application/xhtml+xml` → `web`.
5. Any other `text/*` (`text/plain`, `text/markdown`, …) → `text`. This is
   why a link to a `.txt`/`.md` file is not run through boilerplate removal,
   which would throw the content away.
6. Anything else: `web` if it came from a URL, otherwise `text`.

**Two behaviours worth knowing when you touch this path:**

- *The modality of a URL is re-detected after the fetch*, against the content
  type the server actually returned. A link to a blog post and a link to a
  PDF are the same input shape; only the response distinguishes them. Without
  the second pass, every `arxiv.org/pdf/...`-style link went to the HTML
  extractor and failed with "no readable content".
- *The duplicate check runs before the fetch.* `source_id` for a URL is a
  hash of its canonical form (`layout.canonical_url`), so re-capturing a
  known URL costs no network request. File and text sources are hashed by
  content, so identical bytes — or the identical string pasted twice — are
  the same source.

`meta.title` is filled at capture time and never rewritten (`raw/` is
append-only): from the caller's `title` if given, else the HTML `<title>` /
trafilatura metadata for web, the oEmbed video title for YouTube, and the
first non-empty line for text.

### 3.2 The Incremental Compiler (five stages, `wiki/compiler.py:Compiler`)

This is the core of the design — it exists to satisfy one constraint:
**compilation must never scan the full wiki** (guarded by
`tests/unit/test_compiler_no_full_scan.py`, load-bearing per `CLAUDE.md`).

```
Compiler.compile_source(doc)
  │
  ├─ 1. _summarize(doc)          one LLM call, op="summarize_source"
  │       LLMClient.complete(schema=SUMMARY_SCHEMA)  →  SourceSummary
  │
  ├─ 2. _locate(summary, manifest)   NO LLM call, NO page bodies read
  │       gists.load_gists(store)               → dict[slug, PageGist]  (one object read)
  │       Embedder.embed(probes)  →  VectorStore.query(gists_index)     (gist vectors only)
  │
  ├─ 3. _plan(summary, candidates)   one LLM call, op="plan_compile"
  │       LLMClient.complete(schema=PLAN_SCHEMA)  →  CompilePlan (capped at COMPILE_MAX_PAGES ops)
  │
  ├─ 4. _execute(doc, summary, plan, manifest, result)
  │       for each CompileOp:
  │         _create_page()  → LLMClient.complete(op="create_page")  → wiki.pages.write_page()
  │         _patch_page()   → wiki.pages.read_page() [the ONE counted page-body read]
  │                          → LLMClient.complete(op="patch_page")  → wiki.pages.write_page()
  │       write_page() enforces an optimistic version check (VersionConflict on a race)
  │
  └─ 5. _record(doc, result, manifest)     NO LLM calls
          writes the source note (wiki/sources/{id}.md)
          gists.upsert_gist() + gists.save_gists() + gists.write_index()   (mechanical, no LLM)
          appends CostRecord rows to wiki/_meta/cost.jsonl
```

Every LLM call in the compiler goes through `chains.prompts_loader.load_prompt(name)`
for its `system` prompt and a JSON schema (`SUMMARY_SCHEMA`, `PLAN_SCHEMA`,
`PAGE_SCHEMA` — all defined at the top of `wiki/compiler.py`) forcing
structured output. Global lint/synthesis (`wiki/lint.py:lint_wiki`) is
**not** part of this path — it is the one place allowed to call
`ObjectStore.list()` on `wiki/`, and it only runs from a schedule or
`llmwiki lint`.

### 3.3 Query

```
api/routes.py:answer()  /  mcp does NOT expose this (see §6.3)  /  cli.py "ask" command
      │
      ▼
tools.py:answer(query)  ──►  agent.query.QueryAgent.answer(query)
      │
      ├─ Embedder.embed([query])
      ├─ VectorStore.query(gists_index)             ← wiki search, tried FIRST
      │     if best hit score ≥ WIKI_CONFIDENCE (0.35): wiki alone is used
      │     else: VectorStore.query(chunks_index)    ← RAG fallback, only now
      ├─ _build_context()                            ← UNCHANGED by everything below
      │     wiki.pages.read_page() for each wiki hit's slug
      │     citations built from each page's front_matter.sources
      │     (+ chunk hits' source_id/url if the fallback ran)
      │
      ├─ agent.skills.discover_skills(settings.agent_skills_dir)   [R5, §2.4]
      │     │
      │     ├─ {} (no skills/ directory, or it's empty)
      │     │     └─ _answer_with_fixed_prompt()
      │     │           LLMClient.complete(op="answer_query",
      │     │                              system=load_prompt("answer_query"))
      │     │           ← the ENTIRE pre-R5 behaviour, byte-identical
      │     │
      │     └─ {name: Skill, ...} (≥1 discovered)
      │           └─ _answer_with_skills()
      │                 ├─ _select_skills(query, skills)
      │                 │     LLMClient.complete(op="answer_query",
      │                 │       system=SKILL_SELECTION_SYSTEM,
      │                 │       schema={"skills": enum(discovered names), ...})
      │                 │     invalid/hallucinated choice → retry once →
      │                 │       None ⇒ fall back to _answer_with_fixed_prompt()
      │                 │
      │                 └─ for each chosen skill name, in order (≤ MAX_SKILL_CHAIN):
      │                       LLMClient.complete(op="answer_query",
      │                         system=skills[name].body,     ← NOT load_prompt()
      │                         prompt=question + context [+ previous step's text])
      │                     → last step's text is the answer
      │
      └─ resolved = [c for c in citations if self.source_exists(c.source_id)]
            source_exists() checks ObjectStore.exists(raw/{id}/meta.json)
            → Answer{text, citations, used_rag_fallback}
```

The citation-resolution step is what
`tests/unit/test_agent.py::test_every_citation_resolves_to_a_real_raw_object`
guards (load-bearing per `CLAUDE.md`): an `Answer` can never cite a source
that isn't really in `raw/`. Note where it sits in the diagram above — **after**
`_build_context()` and **after** every skill-invocation branch rejoins — which
is why R5 needed no change to that guard: citations are a property of what
was *retrieved*, never of which skill (or how many LLM calls) produced the
final text. See §2.4 for the full agent-vs-`chains/`-vs-`skills/` picture,
§3.6 for the four `LLMClient` implementations and how each `system=` string
above is sourced, and §5.8 for the extension guide.

### 3.4 One function surface, three transports

`tools.py` defines every operation once. `api/routes.py`, `mcp/server.py`,
and `cli.py` each call the *same function objects* — `test_layering.py`'s
`test_transport_layer_only_calls_tools` enforces that these three modules
import nothing from `storage/extractors/embedding/vector/llm/pipeline/factory`
directly, and `tests/unit/test_tools_and_mcp.py` separately asserts the REST
and MCP surfaces cannot drift apart. See §6 for the full surface.

### 3.5 LLM call resolution (single-provider vs. multi-provider routing)

Every call site in the domain layer (`wiki/compiler.py`, `agent/query.py`)
calls `LLMClient.complete(op=..., system=..., prompt=..., schema=...)` — it
never knows or cares which concrete client answers it. `factory.llm_client()`
decides that once, at construction time:

```
factory.llm_client(cfg)
  └─► factory._build_llm_client(cfg)
        └─► llm.routing_config.load_routing_config(cfg.llm_providers_config, cfg.llm_ops_config)
              │
              ├─ config/providers.py + config/ops.py BOTH exist
              │     → RoutingConfig{providers, ops}
              │     → factory._build_routed_llm_client(cfg, routing)
              │           builds one concrete client per *active* provider
              │           → llm.router.RoutingLLMClient(routing, clients)
              │                 .complete(op=...) looks up routing.ops[op] → dispatches
              │                 to the right client with that op's model/temperature/max_tokens
              │
              └─ neither file exists (the default, zero-config case)
                    → single-provider fallback, driven entirely by Settings
                    → factory._construct_provider_client(cfg.llm_provider, ...)
                          "anthropic" → llm.anthropic_client.AnthropicLLM     (native: caching, cost)
                          other       → llm.langchain_client.LangChainLLM    (via llm.providers.build())
                          "fake"      → llm.fake.FakeLLM
```

See §5 for how to extend either path.

### 3.6 `LLMClient` implementations, and how a prompt/skill body reaches one

§3.5 showed *which client gets built*. This section shows the other half:
*what text ends up inside that client's `system=` argument*, and ties the two
together into one picture. There are exactly four classes that satisfy the
`LLMClient` Protocol (`llm/base.py:30`) — every one of them can execute a
`chains/prompts/*.md` or `skills/*.md` body, because by the time either
reaches `.complete()` it is just a plain `str`; the client has no idea which
file (or which mechanism, §2.4) it came from.

| Class | File | Backend | Built when |
|---|---|---|---|
| `AnthropicLLM` | `llm/anthropic_client.py:61` | Native Anthropic SDK — prompt caching, measured USD cost (plan §7.5) | Fallback mode with `LLM_PROVIDER=anthropic`, or a routed op whose `config/ops.py` row names provider `anthropic` |
| `LangChainLLM` | `llm/langchain_client.py:69` | Wraps a LangChain chat model — `openai`/`google`/`nvidia`/`deepseek`/`openrouter`, each behind its own extra | Fallback mode with `LLM_PROVIDER` set to one of those five, or a routed op naming one of them |
| `FakeLLM` | `llm/fake.py:24` | Offline double — synthesizes deterministic text/JSON, no network, no cost | `LLM_PROVIDER=fake` (tests, `--offline`), or a routed op naming provider `fake` |
| `RoutingLLMClient` | `llm/router.py:15` | Not a real backend — holds a `dict[provider, LLMClient]` (one real client per *distinct* provider in use) and dispatches `.complete(op=...)` to the right one per `config/ops.py`'s row for that `op` | Only when **both** `config/providers.py` and `config/ops.py` exist (R1–R3, §5.6) |

**The combined flow**, construction (left, once per process — `factory.py`'s
`_cached()` memoizes by config key) feeding into prompt/skill sourcing (right,
every call):

```
                         CONSTRUCTION                                        EVERY CALL
                    (factory.llm_client(cfg), cached)                  (wiki/compiler.py or
                                                                          agent/query.py)
routing_config.load_routing_config()
      │
      ├─ config/providers.py + config/ops.py BOTH exist
      │     └─► RoutingLLMClient{ops→provider→client}      ─┐
      │           one AnthropicLLM/LangChainLLM/FakeLLM      │
      │           per distinct provider named in ops.py      │
      │                                                       │
      └─ neither exists (default)                             ├──► self.llm  (one LLMClient,
            └─► single AnthropicLLM / LangChainLLM / FakeLLM  ┘      held by Compiler or
                  chosen by LLM_PROVIDER alone                       QueryAgent for its lifetime)
                                                                             │
                                                                             │  .complete(op=, system=, prompt=, schema=)
                                                                             ▼
   ┌─────────────────────────── system= is sourced BEFORE this call, by the caller ──────────────────────────┐
   │                                                                                                          │
   │  wiki/compiler.py, ALWAYS:                          agent/query.py, decided by discover_skills() first: │
   │    load_prompt("summarize_source"|                                                                      │
   │                "plan_compile"|                        no skills/ catalog:                               │
   │                "create_page"|                           load_prompt("answer_query")                     │
   │                "patch_page")                                                                             │
   │    ← chains/prompts_loader.py                         catalog present:                                  │
   │      reads chains/prompts/{name}.md,                    1. SKILL_SELECTION_SYSTEM (literal Python        │
   │      strips YAML frontmatter,                              string, not a file) — one forced-schema call  │
   │      returns body only, @cache'd                        2. skills[chosen_name].body, per chosen skill    │
   │                                                             ← agent/skills.py:discover_skills() reads    │
   │                                                               skills/*.md, strips frontmatter the same   │
   │                                                               way, returns {name: Skill(body=...)}       │
   └──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                                             │
                                                                             ▼
                                                        LLMResponse{text, data, usage} → CostRecord{op=...}
                                                        appended to wiki/_meta/cost.jsonl (op label only —
                                                        does not distinguish provider, or skill-selection
                                                        vs. skill-generation calls; §2.4 gotchas)
```

Two things worth internalizing from this diagram:

- **Which concrete class runs a given call and which text it runs are decided
  independently, by different code, at different times.** The left half
  (`factory.py`) only ever asks "which provider does this `op` use, right
  now, for this process". The right half (`chains/prompts_loader.py` /
  `agent/skills.py`) only ever asks "which file's body is `system=`, for this
  call". Neither side reads the other's decision — a routed `answer_query`
  call going to `openai` still sources its `system` text from
  `chains/prompts/answer_query.md` or `skills/*.md` exactly as it would under
  `anthropic`.
- **`RoutingLLMClient` never touches a prompt file itself.** It only ever
  looks at `op` (a string like `"answer_query"`) to decide *which client
  object* to forward to — the `system=`/`prompt=` strings pass through it
  unread. This is why adding a new routed provider (§5.6) or a new skill file
  (§5.8) never requires touching `llm/router.py`.

See §5.1–§5.2 for adding a new `LLMClient` implementation, §5.3 for a new
`op=` value (compiler side), and §5.8 for a new `skills/` file (query-agent
side, no code change).

---

## 4. Data Model Reference (`models/`)

All schemas are pydantic `BaseModel`s, L0 (no I/O, no imports from anywhere
else in `llmwiki`). This is the vocabulary every layer above speaks.

| Model | File | Purpose |
|---|---|---|
| `SourceMeta` | `models/source.py` | Written once to `raw/{id}/meta.json`, never mutated. |
| `SourceRef` | `models/source.py` | What `ingest_source` returns immediately (`source_id`, `status`, `duplicate`). |
| `SourceStatus` | `models/source.py` | Pipeline progress, polled via `GET /sources/{id}`. |
| `ExtractedDoc` | `models/source.py` | Normalized text from an extractor, before chunking. |
| `Chunk` / `ChunkMetadata` | `models/chunk.py` | One embeddable slice; `Chunk.make_id()` is deterministic so re-ingest overwrites, never duplicates. |
| `SearchHit` | `models/chunk.py` | One retrieval result, from either the gist or chunk index. |
| `PageFrontMatter` / `WikiPage` | `models/page.py` | The YAML block + body of every wiki page. |
| `PageGist` | `models/page.py` | One row of `wiki/_meta/gists.json` — the progressive-disclosure index; this is what the compiler reads instead of page bodies. |
| `LintFinding` / `LintReport` | `models/page.py` | Output of the scheduled global lint. |
| `CompileOp` / `CompilePlan` | `models/plan.py` | The planner's output — sees gists only, never page bodies. |
| `SourceSummary` | `models/plan.py` | The compiler's only view of the raw source text. |
| `CompileResult` | `models/plan.py` | What one compile pass did — returned by `compile_update`. |
| `CostRecord` | `models/plan.py` | One line of `wiki/_meta/cost.jsonl` — fields are effectively stable, since they're serialized to disk and read back. |
| `CostSummary` | `models/plan.py` | Aggregation for `llmwiki cost`. |
| `Citation` / `Answer` | `models/plan.py` | The query agent's output; every `Citation` is checked to resolve. |

`wiki/_meta/gists.json` deserves emphasis: it is the single object that lets
the compiler, `list_concepts`, and `wiki/index.md` rendering all avoid
scanning `wiki/` — a cost that would otherwise grow with corpus size.

---

## 5. Extension Guide

### 5.1 Adding a new LLM provider (via LangChain — the common case)

This is the path for any provider LangChain already integrates
(`langchain-*` package exists). Follow `llm/providers.py`'s existing entries
as the template.

1. **`src/llmwiki/llm/providers.py`** — add a `ProviderSpec` row to
   `REGISTRY`:
   ```python
   "mistral": ProviderSpec(
       "langchain_mistralai", "ChatMistralAI", "mistral", "langchain-mistralai"
   ),
   ```
   Set `max_tokens_arg="..."` only if that integration's constructor spells
   the token cap differently than `max_tokens` (see `openai`'s
   `max_completion_tokens` for precedent). Do **not** import the provider
   module at the top of the file — `load_class()` imports it function-locally
   inside `build()`, which is what keeps `pip install llmwiki` free of every
   provider SDK; `tests/unit/test_providers.py::test_importing_the_registry_imports_no_provider_sdk`
   is load-bearing per `CLAUDE.md` and will fail if you break this.

2. **`src/llmwiki/config.py`** — add the literal to `Provider`:
   ```python
   Provider = Literal["anthropic", "fake", "openai", "google", "nvidia",
                       "deepseek", "openrouter", "mistral"]
   ```

3. **`pyproject.toml`** — add the extra:
   ```toml
   mistral = ["llmwiki[langchain]", "langchain-mistralai"]
   all-providers = ["llmwiki[openai,google,nvidia,deepseek,openrouter,mistral]"]
   ```

4. **`.env.example`** — add a row to the provider table comment (this is
   mandated by `CLAUDE.md` §"`.env.example` is mandatory and must stay in
   sync" independent of this repo's own conventions).

5. **`config/providers.py.example`** — add a commented-out row so a
   multi-provider deployment can enable it by uncommenting.

6. **`src/llmwiki/llm/pricing.py`** — optional but recommended: add
   `RATES` entries for the models you'll actually use, or every call through
   this provider records `cost_usd = 0.0` (which means "unpriced", not
   "free" — see the module docstring).

7. **Tests** — `tests/unit/test_providers.py` already parametrizes over
   `REGISTRY`, so a correctly-shaped new entry is exercised automatically
   (skipped if the extra isn't installed). No new test file needed unless the
   provider's constructor genuinely disagrees with the shared keyword
   contract (`model`, `api_key`, `base_url`, `temperature`) — if it does,
   that disagreement is exactly what `test_registry_keywords_match_the_installed_class`
   checks for.

No change to `factory.py`, `llm/router.py`, `wiki/compiler.py`, or
`agent/query.py` is needed — the domain layer only ever sees `LLMClient`.

### 5.2 Adding a new LLM provider with a *native* adapter (like Anthropic)

Only do this if you need something LangChain's generic surface can't give
you (Anthropic's case: prompt caching via `cache_control`, and precise
`cache_read`/`cache_write` token accounting). Otherwise prefer §5.1.

1. Create `src/llmwiki/llm/<provider>_client.py`, implementing `LLMClient`
   (the `complete()` signature in `llm/base.py`). Use `anthropic_client.py`
   as the template: import the SDK inside `__init__` (never at module level),
   build a `CostRecord` from the SDK's real usage fields, price it via
   `llm/pricing.py`.
2. Wire it into `factory._construct_provider_client()` alongside the
   `"anthropic"` branch.
3. Add the literal to `config.Provider`.
4. `cfg.require(...)` for whatever credentials it needs, mirroring the
   Anthropic branch's pattern in `factory._build_llm_client`.

### 5.3 Adding a new compiler/agent operation (a new `op=` value)

The five existing ops (`summarize_source`, `plan_compile`, `create_page`,
`patch_page`, `answer_query`) are enumerated in exactly one place that
matters for validation: `llm/routing_config.py:KNOWN_OPS`. If you add a sixth
call site that does `llm.complete(op="new_thing", ...)`:

1. Add `"new_thing"` to `KNOWN_OPS` in `llm/routing_config.py`.
2. Add a row for it to `config/ops.py.example` (and any real
   `config/ops.py` a deployment has — `_resolve_ops` fails loudly at startup
   if a known op has no row, by design).
3. If it needs a prompt template, add `chains/prompts/new_thing.md` and call
   `load_prompt("new_thing")` at the call site.
4. Existing single-provider fallback needs no change — `Settings.llm_model`/
   `llm_max_tokens`/`llm_temperature` apply uniformly regardless of `op`.

### 5.4 Adding a new extractor (modality)

1. Create `src/llmwiki/extractors/<modality>.py` with a class implementing
   `Extractor` (`extract(meta, data) -> ExtractedDoc` — see `extractors/base.py`).
   Concrete parsing libraries import function-locally, matching every
   existing extractor.
2. Add the modality to `models/source.py:Modality` (a `Literal`).
3. Wire detection into `extractors/base.py:detect_modality()` — mind the
   ordering rules in §3.1.1. Note the function is called **twice** for a URL
   source (once on the declared mime to choose a fetch strategy, once on the
   served content type), so it must be a pure function of its arguments and
   tolerate an empty `mime`.
4. Wire dispatch into `extractors/base.py:get_extractor()`.
5. If the modality needs a network fetch at capture time (like `web.py`'s
   `fetch()` or `youtube.py`'s `fetch_transcript()`), add that function here
   and call it from `pipeline/ingest.py:IngestPipeline._fetch()`. Capture
   must store the fetched bytes under `raw/` first — extraction is required
   to stay a pure function of stored bytes, so it can be re-run offline
   later without hitting the network again.
6. Add a fixture under `tests/fixtures/` and a case in
   `tests/unit/test_extractors.py`.

### 5.5 Adding a new storage or vector backend

Both follow the same shape as §5.1/§5.4: implement the `Protocol`
(`storage/base.py:ObjectStore` or `vector/base.py:VectorStore`), add a
`Literal` value to `config.Settings.storage_backend` / `.vector_backend`,
and wire a branch into `factory._build_object_store()` /
`factory._build_vector_store()`, importing the concrete SDK function-locally.
`tests/unit/test_store_contract.py` and `test_vector_contract.py` run **one**
shared contract test against every registered implementation — add yours to
their parametrization rather than writing a parallel test file, so it's held
to the same behavioural contract (idempotent upsert, `ObjectNotFound` on a
missing key, etc.) automatically.

### 5.6 Multi-provider / per-op LLM routing (deployment-time extension, no code change)

This is not a code extension but a **deployment configuration** extension —
worth documenting here because it's the mechanism that makes §5.1's new
providers actually usable in combination. It exists outside `src/llmwiki`
entirely (design v1.4 §4.8: this is *this application's* concern, not part
of the shareable LLM contract).

```bash
cp config/providers.py.example config/providers.py
cp config/ops.py.example config/ops.py
```

- `config/providers.py` defines `PROVIDERS: list[dict]` — which providers are
  available and which **environment variable name** (not value) carries each
  one's credentials. It holds no secrets.
- `config/ops.py` defines `OPS: list[dict]` — one row per op in `KNOWN_OPS`
  (§5.3), naming provider/model/temperature/max_tokens.
- **Presence of both files is the switch.** Absent (the default, a fresh
  clone) → today's single-provider path from `Settings` — zero behaviour
  change. Present → `llm/routing_config.load_routing_config()` builds a
  `RoutingConfig`, and `factory._build_routed_llm_client()` constructs one
  concrete client per *active* provider and wraps them in
  `llm.router.RoutingLLMClient`.
- A provider row whose `api_key_env` resolves to empty (unset in both the
  real environment and `.env`) is silently **inactive**, not an error — only
  an `ops.py` row that names an inactive/undefined provider fails, loudly, at
  load time. Same "fail by name" posture as `Settings.require`.
- **Gotcha, already hit once (see `HISTORY.md` 2026-09-06):** once
  `config/providers.py` exists in the checkout, it also governs *this
  repository's own* `pytest` run and `scripts/smoke_flow.py --offline`,
  because both build a default `Settings()` that resolves the same relative
  path. `tests/conftest.py` has an autouse fixture that points both config
  paths at a nonexistent path for every test, and `--offline` does the same
  — so this is handled, but if you ever see a test unexpectedly trying real
  network calls, check whether that guard got bypassed (e.g. a test
  constructing `Settings` some new way).
- Credential resolution precedence: a real `os.environ` value wins over one
  read from `.env` (via `dotenv_values()`, which never mutates
  `os.environ`) — matching pydantic-settings' own source order. This is why
  `llm/routing_config.py` is the *second* module (after `config.py`) allowed
  to touch environment/`.env` state directly: a provider row's
  `api_key_env` names an arbitrary variable not known until the file itself
  is read, so `Settings`' fixed fields can't cover it.

### 5.7 Adding a new tool / API endpoint / MCP tool

1. Write the function in `tools.py`. It should call one or two collaborators
   (`_pipeline(cfg)`, `_agent(cfg)`, or `factory.*` directly) and contain no
   business logic itself beyond wiring — the logic belongs in `wiki/`,
   `agent/`, or `pipeline/`.
2. To expose it over REST: add a route in `api/routes.py` that calls it and
   nothing else (`test_transport_layer_only_calls_tools` will fail the build
   if you import a domain module here directly).
3. To expose it to agents over MCP: add an `@mcp.tool()` function in
   `mcp/server.py:build_server()` that calls the same `tools.py` function,
   serializing pydantic results with `.model_dump(mode="json")`. **Think
   before doing this** — the design deliberately keeps the MCP surface to
   six tools (`search_wiki`, `get_page`, `ingest_source`, `compile_update`,
   `list_concepts`, `lint_wiki`) on the stated reasoning that "a small,
   well-named tool set is easier for a model to use correctly than a large
   one" (`mcp/server.py` docstring). Helper operations (`answer`, status,
   cost) are intentionally REST/CLI-only.
4. To expose it on the CLI: add a subparser in `cli.py:build_parser()` and a
   branch in `main()`.
5. Add a test: for REST, a case in `tests/unit/test_routes.py`; for MCP
   parity, `tests/unit/test_tools_and_mcp.py` already asserts the tool
   registry and REST reach the same function objects — extend its assertions
   if you add or remove a tool.

### 5.8 R4/R5: SKILL.md prompts and query-agent skill invocation

Landed 2026-09-07 (`HISTORY.md`); design v1.4 §4.8.2, `implement-plan-v1.4.md`
§19.4/§19.5. See §2.4 for the conceptual picture (agents vs `chains/prompts/`
vs `skills/`) and §3.3 for the annotated call-flow diagram; this section is
the "how to add one" complement to those.

- **R4 — SKILL.md-format prompts.** The five files under
  `chains/prompts/*.md` carry real YAML frontmatter (`name`, `description`),
  making each independently a valid [Agent
  Skill](https://code.claude.com/docs/en/skills), discoverable by an external
  harness (Claude Code, the Claude Agent SDK, an MCP client), not only by
  this codebase's own `load_prompt()` (`chains/prompts_loader.py`, which
  parses and discards the frontmatter — every existing caller still gets the
  body only). The four **compiler** stages keep their deterministic, fixed
  op→prompt mapping — unchanged, and still what §4.4's cost-bounded,
  non-agentic compilation guarantee depends on.
- **R5 — query-agent skill invocation.** The **query agent only**
  (`agent/query.py:QueryAgent.answer()`) has genuine skill invocation. After
  retrieval and context-building (unchanged), it calls
  `agent/skills.py:discover_skills(settings.agent_skills_dir)` — SKILL.md
  files under a repo-root `skills/` directory (`AGENT_SKILLS_DIR`, default
  `./skills`; this repository ships `answer-query` and `compare-concepts`).
  No skills discovered → the pre-R5 fixed-prompt call, byte-identical. Skills
  discovered → one `answer_query`-op call, forced by a `schema` naming the
  discovered skill names (the same forced-tool-call shape the compiler
  already uses for structured output — no new op, no `LLMClient` protocol
  change), asks the model to pick one skill or an ordered chain of up to
  `MAX_SKILL_CHAIN` (3); each chosen skill's frontmatter body becomes the
  system prompt for one more `answer_query` call, later steps receiving the
  previous step's output appended to the prompt. A selection that names
  nothing from the discovered set is retried once, then falls back to the
  fixed `answer_query` skill (§19.9 item 2) — a malformed or hallucinated
  choice must not be a hard failure on a user-facing query. Citation
  resolution (`source_exists`, the load-bearing contract) sits above all of
  this and is unaffected by which skill produced the text.
- Tests: `tests/unit/test_prompts_loader.py` (R4),
  `tests/unit/test_agent_skill_invocation.py` (R5 — discovery, selection,
  chaining, the fallback path, and the citation contract re-run specifically
  against a skill-invoked answer). `tests/conftest.py`'s
  `_isolate_agent_skills_dir` keeps every other test in the suite on the
  fixed-prompt path, mirroring R1's `_isolate_llm_routing_config`.

**Recipe: adding a new query-agent skill (no code change).**

1. Write a new file under `skills/`, e.g. `skills/summarize_topic.md`:
   ```markdown
   ---
   name: summarize-topic
   description: Give a single-page overview of everything the wiki knows about one topic.
   ---

   You are summarizing everything a compiled knowledge base contains about one topic...
   ```
2. That's it — the next `QueryAgent.answer()` call's `discover_skills()`
   picks it up automatically; no import, no registration, no restart-required
   config. Compare to §5.3 ("adding a new compiler/agent **operation**"),
   which *does* require a code change — the two are different extension
   points on purpose (§2.4).
3. To test it deliberately (rather than leaving it to the model's judgment),
   write a case in `tests/unit/test_agent_skill_invocation.py` using
   `SequencedLLM` (scripts the selection call's response), following the
   existing `test_the_models_skill_choice_is_honored` pattern — pass your own
   `agent_skills_dir=` since the suite's default isolates this away (see the
   gotcha in §2.4).
4. `name` must be unique across every file in the directory (`discover_skills`
   skips, and logs, a duplicate — first one wins, sorted by filename) and
   present (a file missing `name` is skipped and logged, not fatal).

---

## 6. API Reference

### 6.1 Python core layer (`llmwiki.tools`)

This is the layer other Python code — a notebook, a script, another service
(design v1.4 names `FUND-financial-Research` as an intended cross-repo
consumer, see `implement-plan-v1.4.md` §11) — should import directly, rather
than going through HTTP. `pip install -e ".[dev]"` and:

```python
from llmwiki import tools
from llmwiki.config import load_settings

cfg = load_settings()             # or pass cfg=None to use the process-wide default
result = tools.search_wiki("retrieval augmented generation", k=5, cfg=cfg)
```

Every function takes an optional `cfg: Settings | None = None` — omit it to
use the module-level default `Settings()` built from `.env`/environment at
import time, or pass an explicit `Settings(...)` to point at different
backends without touching the environment (this is exactly how the test
suite and the CLI's `--offline` flag work).

**The six canonical tools** (also the entire MCP surface, §6.3):

| Function | Signature | Returns | Notes |
|---|---|---|---|
| `search_wiki` | `(query: str, k: int = 5, cfg=None)` | `list[SearchHit]` | Wiki-first; chunk fallback only if the top wiki hit is weak. |
| `get_page` | `(slug: str, cfg=None)` | `WikiPage` | Raises `tools.PageNotFound` (a `KeyError`) if absent. |
| `ingest_source` | `(url=None, file=None, filename=None, mime="", title="", text=None, cfg=None)` | `SourceRef` | Capture only — returns before extraction/compilation run. Exactly one of `url`/`file`/`text` (§3.1.1). |
| `compile_update` | `(source_id: str, force: bool = False, cfg=None)` | `CompileResult` | Re-runs the 5-stage compiler for an already-captured source. |
| `list_concepts` | `(prefix: str \| None = None, cfg=None)` | `list[PageGist]` | One object read (`gists.json`); no LLM call regardless of wiki size. |
| `lint_wiki` | `(dry_run: bool = True, cfg=None)` | `LintReport` | The only function allowed to scan all of `wiki/`. Not on the ingest path. |

**Helper functions** (Python/REST/CLI, deliberately **not** MCP tools):

| Function | Signature | Returns | Notes |
|---|---|---|---|
| `ingest_now` | `(url=None, file=None, filename=None, mime="", title="", text=None, cfg=None)` | `SourceStatus` | Capture **and** process, synchronously. Used by CLI and the smoke script. |
| `process_source` | `(source_id: str, cfg=None)` | `SourceStatus` | The expensive half of ingest; called by the API's background task. |
| `get_source_status` | `(source_id: str, cfg=None)` | `SourceStatus` | Poll pipeline state. |
| `answer` | `(query: str, k: int = 5, cfg=None)` | `Answer` | Full RAG answer with verified citations. |
| `source_exists` | `(source_id: str, cfg=None)` | `bool` | True iff a real object exists under `raw/{id}/`. |
| `cost_summary` | `(since: datetime \| None = None, cfg=None)` | `CostSummary` | Aggregates `wiki/_meta/cost.jsonl`. |
| `delete_source` | `(source_id: str, cfg=None)` | `int` | Removes `raw/` objects, status, and vectors for a source. Leaves compiled pages alone. |
| `health` | `(cfg=None)` | `dict` | No network calls — just reports configured backends/version. |

### 6.2 REST API (`llmwiki.api`, `api/routes.py`)

Base: whatever `llmwiki serve` binds (`API_HOST`/`API_PORT`, default
`0.0.0.0:8000`). Auth: a single static bearer token (`INGEST_API_TOKEN`),
checked with `secrets.compare_digest` — Phase 0 only; see design doc for the
Phase 1 auth plan. `Authorization: Bearer <token>` is required on the routes
marked 🔒 below.

| Method & path | Auth | Body / Query | Response model | Calls |
|---|:-:|---|---|---|
| `GET /healthz` | — | — | `dict` | `tools.health()` |
| `POST /ingest` | 🔒 | JSON `{url, title?}` **or** `{text, title?}` | `SourceRef` | `tools.ingest_source()` + background `process_source`. Exactly one of `url`/`text`, enforced by a model validator → 422. |
| `POST /upload` | 🔒 | multipart `file`, `title?` | `SourceRef` | same, for the file kinds (PDF, `.txt`/`.md`, HTML, image) |
| `GET /sources/{source_id}` | — | — | `SourceStatus` | `tools.get_source_status()` |
| `GET /search` | — | `q`, `k=5` | `list[SearchHit]` | `tools.search_wiki()` |
| `GET /answer` | — | `q`, `k=5` | `Answer` | `tools.answer()` |
| `GET /concepts` | — | `prefix?` | `list[PageGist]` | `tools.list_concepts()` |
| `GET /page/{slug}` | — | — | raw markdown (`text/plain`) | `tools.get_page()` → `wiki.pages.render_page()`; 404 if absent |
| `POST /compile/{source_id}` | 🔒 | `force=false` | `CompileResult` | `tools.compile_update()` |
| `POST /lint` | 🔒 | `dry_run=true` | `LintReport` | `tools.lint_wiki()` |

OpenAPI/Swagger is auto-generated by FastAPI at `/docs` (interactive) and
`/openapi.json` while the service is running.

### 6.3 MCP tools (`llmwiki.mcp`, `mcp/server.py`)

Mounted inside the same FastAPI process at `/mcp` (`api/app.py` builds the
MCP ASGI app and mounts it — note the `path="/"` / mount-at-`/mcp` detail
recorded in `HISTORY.md`, easy to regress if you touch this file). Also
runnable standalone over stdio: `python -m llmwiki.mcp.server`.

Exactly six tools, deliberately no more (§5.7):

| MCP tool | Args | Wraps |
|---|---|---|
| `search_wiki` | `query: str, k: int = 5` | `tools.search_wiki` |
| `get_page` | `slug: str` | `tools.get_page` |
| `ingest_source` | `url: str \| None = None, text: str \| None = None, title: str = ""` | `tools.ingest_source` (and synchronously runs `process_source` before returning — MCP has no background-task concept here). `url` covers blog/YouTube/PDF links; `text` stores a pasted string verbatim; files go over REST `/upload`. |
| `compile_update` | `source_id: str, force: bool = False` | `tools.compile_update` |
| `list_concepts` | `prefix: str \| None = None` | `tools.list_concepts` |
| `lint_wiki` | `dry_run: bool = True` | `tools.lint_wiki` |

### 6.4 CLI (`llmwiki`, `cli.py`)

```
llmwiki [--offline] <command> [args]
```

| Command | Args | Calls |
|---|---|---|
| `ingest` | `--url URL \| --file PATH \| --text STR` (`--text -` reads stdin), `--title` | `tools.ingest_now` |
| `search` | `query`, `-k N` | `tools.search_wiki` |
| `ask` | `query` | `tools.answer` |
| `page` | `slug` | `tools.get_page` (prints rendered markdown) |
| `concepts` | `--prefix` | `tools.list_concepts` |
| `compile` | `source_id`, `--force` | `tools.compile_update` |
| `lint` | `--fix` | `tools.lint_wiki` |
| `cost` | — | `tools.cost_summary` |
| `status` | — | `tools.health` |
| `source` | `source_id` | `tools.get_source_status` |
| `serve` | `--host`, `--port`, `--reload` | runs `uvicorn` against `llmwiki.api.app:app` |

`--offline` (must precede the subcommand) forces
`STORAGE_BACKEND=local VECTOR_BACKEND=memory EMBEDDING_BACKEND=fake
LLM_PROVIDER=fake` before `Settings` is built.

### 6.5 Configuration reference (`config.Settings`)

`Settings` (pydantic-settings, `env_file=".env"`, case-insensitive,
`extra="ignore"`) is the single source of runtime configuration. Full,
current variable list and defaults live in `.env.example` — treat that file,
not this table, as authoritative, since it is what `CLAUDE.md` mandates be
kept in sync with the code. In summary, grouped:

- **LLM (provider-generic contract):** `LLM_PROVIDER`, `LLM_API_KEY`,
  `LLM_MODEL`, `LLM_BASE_URL`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`.
- **Multi-provider routing (§5.6):** `LLMWIKI_PROVIDERS_CONFIG`,
  `LLMWIKI_OPS_CONFIG` (paths; default `./config/{providers,ops}.py`),
  `AGENT_SKILLS_DIR` (query-agent skill directory, default `./skills`, §5.8).
- **Deprecated LLM aliases** (still read, removed at a future milestone):
  `ANTHROPIC_API_KEY`, `LLM_DEFAULT_MODEL`, `LLM_BACKEND`.
- **Observability:** `LOG_LEVEL`; `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`,
  `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`.
- **Cloudflare:** `CF_ACCOUNT_ID`, `CF_API_TOKEN`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT_URL`,
  `VECTORIZE_CHUNKS_INDEX`, `VECTORIZE_GISTS_INDEX`, `EMBEDDING_MODEL`,
  `EMBEDDING_DIM`.
- **Service:** `INGEST_API_TOKEN`, `API_HOST`, `API_PORT`.
- **Cost guardrails:** `COMPILE_MAX_PAGES`, `COMPILE_CANDIDATE_PAGES`,
  `INGEST_TOKEN_BUDGET`, `CHUNK_SIZE_CHARS`, `CHUNK_OVERLAP_CHARS`.
- **Backend selection:** `STORAGE_BACKEND` (`r2|local`), `VECTOR_BACKEND`
  (`vectorize|memory`), `EMBEDDING_BACKEND` (`workers_ai|fake`),
  `LOCAL_STORAGE_PATH`.

`Settings.require("field_a", "field_b")` is what adapters call in their
constructors to fail loudly, by name, on missing credentials — use it as the
template for any new adapter's precondition check (§5.5).

---

## 7. Object Storage Layout (`storage/layout.py`)

The **only** module that builds object keys — never construct a key by hand
elsewhere.

```
raw/{source_id}/original.{ext}     immutable, written once at capture
raw/{source_id}/meta.json          immutable, written once at capture
raw/{source_id}/extracted.md       the one rewritable object under raw/ (re-run of extraction)
status/{source_id}.json            pipeline progress, polled by GET /sources/{id}
wiki/concepts/{slug}.md            compiled concept pages
wiki/entities/{slug}.md            compiled entity pages
wiki/sources/{source_id}.md        one source note per captured source
wiki/index.md                      mechanically regenerated on every compile — no LLM call
wiki/_meta/gists.json              the manifest — one-line gist per page, read instead of bodies
wiki/_meta/cost.jsonl              append-only cost ledger, one CostRecord per LLM call
```

`source_id` is a 16-hex-char SHA-256 prefix (content address for files,
canonical-URL address for URLs — see `source_id_for_bytes`/`source_id_for_url`)
so re-capturing identical content or the same URL twice always resolves to
the same source and never duplicates. `slugify()` is the one path from an
arbitrary title to a filesystem/Obsidian-safe key; no caller-supplied string
reaches a key unsanitized.

---

## 8. Testing

```bash
pytest                                  # unit tests, ~315 tests, no network, ~5s
pytest -m integration                   # needs a populated .env; costs money
python scripts/smoke_flow.py --offline  # end-to-end, no keys, under 2s
ruff check . && mypy                    # the rest of the pre-commit gate
```

Four tests are **load-bearing** (`CLAUDE.md`) — never weaken them to make a
change pass; if a change genuinely requires touching one, that is a signal
to stop and reconsider the change, not the test:

| Test | Guards |
|---|---|
| `tests/unit/test_layering.py` | The L0–L5 import ladder (§2.1). |
| `tests/unit/test_compiler_no_full_scan.py` | Design §4.4's central cost constraint — no full-wiki scan on the ingest path. |
| `tests/unit/test_agent.py::test_every_citation_resolves_to_a_real_raw_object` | The answer-with-citations contract (§3.3). |
| `tests/unit/test_providers.py::test_importing_the_registry_imports_no_provider_sdk` | `pip install llmwiki` stays free of every LLM provider SDK. |

Other tests worth knowing about when extending a specific seam: the
**contract tests** — `test_store_contract.py`, `test_vector_contract.py` —
run one shared behavioural suite against every registered
backend/implementation (§5.5); add a new backend there rather than writing a
parallel file. `test_tools_and_mcp.py` asserts REST/MCP surface parity
(§5.7). `test_routing_config.py` and `test_router.py` cover the multi-provider
routing feature (§5.6) in isolation, including the `.env`-fallback and
autouse-isolation-fixture behaviour described there.

`tests/doubles.py` and `tests/factories.py` hold shared spies/fakes and
object-builders used across the unit suite — check there before writing a
new one, most scenarios (a spy store that counts reads, a scripted LLM with
canned per-op responses) already exist.

**Known current state:** `pytest` — 315 passed, 5 skipped (provider extras
not installed), 6 integration tests deselected, no failures. The
long-standing failure in
`tests/unit/test_extractors.py::test_fetch_video_title_reads_oembed` was
fixed on 2026-09-08 (the fake `httpx.Response` had no `request` set, so
`raise_for_status()` raised `RuntimeError` and the lookup was swallowed).

---

## 9. Deployment

```bash
docker compose up --build                        # reads .env, real backends
docker compose --profile offline up api-offline   # no keys, fake adapters, :8001
docker compose --profile test run --rm smoke      # runs scripts/smoke_flow.py --offline in the image
docker compose --profile ops run --rm lint        # llmwiki lint — intended as a scheduled job, not on ingest
```

Single-process design (`docker-compose.yml`'s own comment: "there is no
separate worker service to keep in sync, and no queue broker to operate") —
FastAPI serves REST, mounts MCP at `/mcp`, and runs ingest processing in
FastAPI `BackgroundTasks`. `Dockerfile` is a two-stage build (build stage has
compilers/pip cache; runtime stage ships only the built venv, runs as a
non-root user, health-checks `/healthz`).

---

## 10. Known Gap Between This Document, the Design Doc, and the Plan

`docs/implement-plan-v1.4.md` describes an aspirational repository layout
with `packages/agentkit-storage/` and `packages/agentkit-llm/` as
independently-installable distributions (milestones N0–N8). **As of this
document, none of that extraction has happened** — `git status`/the tree
show no `packages/` directory, and `src/llmwiki/llm/`, `src/llmwiki/storage/`
still live where §2.2 describes them. Sections 1–8 of this document describe
the actual, current code; do not assume the `packages/` layout exists when
navigating the repository. If N0–N8 land later, this document (particularly
§2.2's directory table and §5's extension paths) will need a corresponding
rewrite — check `docs/HISTORY.md` for a milestone entry before trusting this
section.

---

*This document is maintained as living Markdown alongside the code. Update it
when a module moves, a layer rule changes, a tool is added or removed from
the MCP/REST/CLI surface, or when the `skills/` catalog (§5.8) changes.*
