# Riphah Voice Agent

A multilingual voice assistant for **Riphah International University**. Ask about
admissions, fee structures, programs, eligibility, and departments — by speaking,
in English, Urdu, Pashto, Punjabi, or Arabic — and get answers grounded in a
knowledge base built from `riphah.edu.pk`. Switch language mid-conversation and
the reply follows.

Built in two phases: a knowledge base first, then the agent on top of it.

```
                7 campus domains          Fee structure API         PDF documents
                (584 sitemap URLs)      (behind a JS dropdown)    (prospectus etc.)
                        │                        │                        │
                        └────────────────┬───────┴────────────────────────┘
                                         ▼
                              Crawler and parser  (weekly, tags campus)
                                         │
                        ┌────────────────┴────────────────┐
                        ▼                                 ▼
                 SQLite tables                      Vector store
        (fees, programs, offerings, dates)   (prose chunks + embeddings)
                        └────────────────┬────────────────┘
                                         ▼
                                   Voice agent
                              (speech in, speech out)
```

## Why a knowledge base and not live web search

Live search per question costs 2–8 seconds. On a phone call that reads as broken.
Retrieval from the local KB is ~100–300 ms, costs nothing per query, and — the
part that actually matters — is the only way to guarantee a quoted fee came from
Riphah's own fee table rather than from a plausible-sounding guess.

## What the crawler found

Every source turned out to be reachable with plain HTTP — no headless browser
needed anywhere.

| Source | How | Yield |
|---|---|---|
| Static pages | `sitemap.xml` | 565 URLs (events/news skipped) |
| Fee structures | `programs/program-finder-fee-structure.php?f=0..8` | **262 fee rows**, 164 programs, 7 campuses |
| Program catalog | `<select name='p'>` on `/admissions/dates/` | **184 programs** (52 UG, 59 grad, 22 PhD, 49 cert/dip, 2 assoc) |
| Offerings | `programs/program-finder-admissions.php?p=<id>` | per-campus intake, seats, timings, open/closed |
| Program details | `programs/detail/?p=<id>` | eligibility + merit criteria, via stable anchor ids |
| Dates & contacts | Claude structured extraction | campaign windows, campus phones |

`robots.txt` permits all of it (only `/cp/`, `/test/`, `/000/`, `/404/` are
disallowed), and the crawler honours those prefixes plus a 0.5 s delay.

### Scope limit worth knowing

The fee and program endpoints cover the **7 campuses** in Riphah's own dropdown
(I-14, G-7 City, Gulberg Green, Al-Mizan, Raiwind, Gulberg, Malakand). But
`/contact/` lists **four more** — Faisalabad, Sahiwal, Peshawar, Gujranwala —
each on its own domain (`riphahfsd.edu.pk`, `riphahsahiwal.edu.pk`,
`riphahpsh.edu.pk`, `riphahgrw.edu.pk`), absent from `riphah.edu.pk`'s sitemap and
absent from the fee dropdown. They're recorded in `config.SATELLITE_CAMPUSES` so
the gap is explicit rather than silent. **Crawling those four domains is the
biggest single coverage win available** — until then the agent has no fee or
program data for roughly a third of Riphah's locations.

## Two things this project is careful about

**1. Currency.** Riphah quotes Pakistani nationals in PKR and international
students in USD *in the same table*. MBBS is `PKR 2,450,000` locally and
`USD 17,000` internationally. Dropping the unit turns the second one into a ~25×
understatement, so `fees.currency` is a first-class column and every amount
crosses the tool boundary pre-formatted with its unit. There is no code path that
hands the model a bare integer.

**2. Fee hallucination.** A voice bot stating a wrong MBBS fee to a prospective
student is a real liability for the university. So: fee questions must go through
`get_fee_structure`; amounts not in a tool result cannot be spoken; totals are
never summed or averaged by the model; every fee answer carries the
first-semester caveat, the recurring-charges note, and the last-verified date.
The eval set includes an adversarial case where the user explicitly asks for a
ballpark — the agent is expected to refuse.

## Setup

```bash
cd Riphah_Voice_Agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # add OPENAI_API_KEY and ANTHROPIC_API_KEY
```

### Phase 1 — build the knowledge base

```bash
python -m kb.build              # ~10 min: crawl, parse, chunk, embed
python -m kb.build --status     # what's in there
```

Individual stages, in dependency order — `pages` → `chunk` → `embed`:

```bash
python -m kb.build --only fees       # just re-pull fee tables (9 requests)
python -m kb.build --only programs   # catalog + offerings + detail pages
python -m kb.build --skip extras     # skip the Claude extraction stage
python -m kb.build --refresh         # ignore the HTML cache, re-fetch everything
python -m kb.build --limit 20        # smoke test
```

Raw HTML is cached under `data/raw/`, so iterating on a parser costs zero
requests to riphah.edu.pk.

### Phase 2 — run the agent

```bash
python -m agent.server            # http://127.0.0.1:8000
```

Open the URL, press **Start talking**, allow the microphone. There's a text box
below the transcript that runs the same tools through Claude — useful when you
want to check an answer without talking.

Terminal REPL, same tools, with the tool trace printed:

```bash
python -m agent.text_agent
```

## Verify it works

```bash
python eval/run_eval.py --retrieval   # 12 KB assertions, no API calls, free
python eval/run_eval.py               # 16 agent cases incl. the currency trap
python eval/run_eval.py --id fee-mbbs-currency-trap
```

Run `--retrieval` while iterating on the crawler: if a fact isn't in the DB, no
amount of prompt work will produce it, so a failure there is always the parser's
problem and never the model's.

## Architecture

```
config.py              campus/faculty/program-type maps + endpoints. Change IDs here only.
kb/
  schema.sql           structured tables + FTS5 index; every row has source_url + fetched_at
  db.py                connections, migrations, crawl_log staging
  fetch.py             polite HTTP with on-disk cache, retry, robots handling
  parse.py             HTML→text, money/currency parsing, sitemap, PDF
  crawl_pages.py       sitemap → pages (content-hash skips unchanged pages)
  crawl_fees.py        fee endpoint → structured fee rows
  crawl_programs.py    catalog + offerings + detail-page sections
  crawl_extras.py      dates + contacts via Claude structured extraction
  chunk.py             heading-aware chunking + synthetic chunks from the tables
  embed.py             OpenAI embeddings → float32 BLOBs
  vector_store.py      in-RAM matrix + cosine search
  retrieve.py          hybrid search (vectors ⊕ FTS5, RRF) + exact structured lookups
  build.py             stage orchestrator / CLI
agent/
  prompts.py           grounding, currency rules, language switching, voice style
  tools.py             7 tools, one schema list → OpenAI + Anthropic wire formats
  server.py            FastAPI: session minting, tool bridge, text chat
  text_agent.py        Claude tool loop for evals and debugging
frontend/index.html    WebRTC voice UI, live transcript, tool-call relay
eval/                  regression set + runner
scripts/refresh.sh     weekly cron entry point
```

### Retrieval is deliberately two-sided

`retrieve.search()` does hybrid retrieval — dense vectors fused with FTS5 keyword
hits via reciprocal rank fusion. Vectors alone miss exact tokens (`BSSE`,
`Pharm-D`); FTS alone misses paraphrase. RRF combines both rankings without
needing the score scales to be comparable.

Everything factual and numeric goes through exact SQL instead
(`fee_structure()`, `program_info()`, …), so a fee is a lookup, not a similarity
match.

### Duplicate-chunk collapsing

Riphah's template repeats whole *sections* across pages — a "Scholarships &
Financial Assistance" blurb sits on 123 of 549 pages. Line-frequency filtering
missed it (22%, under the 25% cutoff), leaving 123 near-identical chunks that
outranked the real scholarships pages for any scholarship-adjacent query: sheer
duplicate mass beats one better match. `chunk.dedupe_chunks()` fixes it at the
source — body text repeated verbatim on 5+ distinct pages is a template block, so
keep one canonical copy and drop the rest. 520 chunks collapsed, and the query
"help paying for my degree if my family cannot afford it" went from returning
program detail pages to returning `/scholarships-financial-assistance/`.

### Conversation history

Voice and text write to one store (`conversations` + `messages`), so a caller can
speak, reload the page, and carry on by typing with the thread intact. The
browser keeps only the conversation id in `localStorage`; the turns live on the
server.

That split exists because **the Realtime API is stateless** — every WebRTC
session starts blank. A dropped call, an expired token, or a closed tab would
otherwise return an agent with no memory of the last five minutes, which on a
phone call is the most obvious possible failure. So turns are persisted as they
happen (batched, with `keepalive` so a closing tab still flushes), and
`conversations.resume_block()` replays the recent transcript into a reconnected
session's instructions, framed as established context so the agent doesn't
re-greet the caller.

Replayed text is flattened first: a text-mode fee answer is full of pipe tables
and bold markers, and handing that to a *voice* model gives it layout it cannot
speak. Tables collapse to `Admission, 11,128. Registration, 3,139.`

Tool turns are stored too, so a replayed transcript shows which lookup produced
an answer, not just the answer.

### The multilingual trick

The knowledge base is English, because the source site is. Rather than translating
the corpus or paying a translation round-trip before every query, the system
prompt instructs the model to **write tool arguments in English regardless of the
spoken language**, then answer in the caller's language. The translation happens
as part of generating the tool call, so it costs no extra latency. A caller says
"MBBS ki fee kitni hai" → `get_fee_structure(program="MBBS")` → answer in Urdu.

### Why the browser calls back for tools

With WebRTC, audio and the data channel run directly between the browser and
OpenAI, so function calls surface in the browser. The browser has no database and
must never hold an API key — so it POSTs the call to `/api/tools/{name}`, this
server runs the query, and the browser returns the result over its data channel.
The OpenAI key stays server-side; the browser only ever holds a ~10-minute
ephemeral credential minted by `/api/realtime/session`, with instructions and
tools baked in server-side so the client can't weaken the guardrails.

### Vector store choice

~3k chunks × 1536 dims × 4 bytes is under 20 MB, and one numpy matmul over that
is ~2 ms — far inside a voice latency budget. So there's no vector database to
run or keep in sync. `vector_store.search()` is the single function to
reimplement if the corpus ever outgrows RAM.

## Keeping it fresh

```bash
crontab -e
0 3 * * 1  /Users/ahsan/Public/Ripah/Ripha_projects/Riphah_Voice_Agent/scripts/refresh.sh
```

Re-crawls, re-embeds only what changed (pages are content-hashed), then runs the
retrieval checks. Fees and deadlines change; a stale KB quotes last year's numbers
with this year's confidence.

## Current state — fully built and verified

| | |
|---|---|
| ✅ 549 pages, 184 programs, 275 offerings, 262 fee rows | crawled into SQLite |
| ✅ **3,367 chunks, all embedded** | 520 duplicate template chunks collapsed first |
| ✅ 3 admission campaign windows, 36 contacts | Claude extraction + regex fallback |
| ✅ **12/12 retrieval checks** | including the PKR/USD currency trap |
| ✅ **16/16 agent cases** | fees, Urdu, adversarial, out-of-scope, escalation |
| ✅ Real ephemeral Realtime token minted (`ek_…`) | the session body — instructions + 7 tools + `semantic_vad` — is accepted by OpenAI |
| ⏳ Live audio round-trip | needs a human at a browser with a microphone |

The one thing left is a person speaking into it. Everything up to the WebRTC
handshake is exercised; the handshake itself needs a real mic.

### Retrieval degrades rather than breaks

With no `OPENAI_API_KEY`, `search()` logs the miss and returns FTS5 keyword hits,
so the agent still answers — noticeably worse on paraphrase, fine on keywords.
That path is worth knowing about: it's what runs if the OpenAI key ever lapses.

## Worth doing next

- **Crawl the four satellite domains** (see *Scope limit* above) — the largest
  coverage gap by a wide margin.
- **Ask Riphah for the source data.** The admissions office almost certainly has
  the prospectus, fee schedule, and eligibility criteria as PDFs or spreadsheets —
  structured, authoritative, and versioned. That's a better KB seed than scraped
  HTML and sidesteps the terms-of-service question. Scrape as a supplement.
- **Mine `query_log`.** Every question is logged with whether the KB had an
  answer. The misses are the to-do list for the next crawl.
- **Telephony.** The Realtime API also runs over SIP, so this could answer an
  actual phone number rather than a web page.
- **Restrict CORS** before this is exposed beyond localhost — `server.py` allows
  all origins for local development.

## Models

| Role | Model | Note |
|---|---|---|
| Voice (speech↔speech) | `gpt-realtime` | native language switching, ~500 ms turns; `semantic_vad` turn detection |
| Embeddings | `text-embedding-3-large` @ 1536d | dimension-reduced to halve storage |
| Extraction + text agent | `claude-opus-5` | no `temperature` — Opus 5 rejects sampling params |
