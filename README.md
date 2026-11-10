# agent-audit

[**English**](README.md) | [**Русский**](README.ru.md)

**Zero-dependency CLI & Agent Skill for auditing OpenAI Codex (`codex`) and Google Antigravity (`agy`) coding sessions.**

`agent-audit` reads local SQLite and JSONL session logs in **strictly read-only mode** and builds a 5-part Markdown or JSON report: real uncached token spend, prompt cache efficiency, context bloat X-ray, repeated manual file/`.env` lookups, and failing tools or polling loops.

No web UI, no background daemon, no external Python packages. You run it from the terminal or ask your coding agent to run it and fix the bottlenecks directly from CLI.

---

## Why This Exists

When you work with CLI coding agents (`codex`, `agy`) across multiple repositories and subagents, raw logs hide where your quota actually goes:

1. **Incompatible token schemas across agents:**
   - In **Codex** (`~/.codex/sessions/**/*.jsonl`), `input_tokens` *already includes* `cached_input_tokens`, `output_tokens` *includes* `reasoning_output_tokens`, and subagent rollouts can duplicate parent events unless deduplicated by `token_usage_record.response_id`.
   - In **Antigravity (`agy`)** (`~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl`), `input_tokens` is *uncached* input only, while `cache_read_tokens` is stored separately.
2. **One bad step costs more than 50 cached steps:** A single `git diff` dumping `80 KB` into the prompt, reading a 3,000-line file without line bounds, or busting the prefix cache on a `120K` context burns more uncached tokens in one turn than an entire normal session.
3. **Agents repeat the same manual digging:** Grepping the same `.env` variables across 15 sessions, re-reading the same file 6 times in a single session, or running `gh pr checks` 10 times in a row instead of using a blocking wait script.

---

## Installation

Requires **Python 3.10+** (standard library only: `sqlite3`, `json`, `pathlib`, `argparse`).

```bash
git clone https://github.com/gmen1057/agent-audit.git
cd agent-audit

# 1. Install CLI binary
chmod +x agent_audit.py
sudo ln -sf "$PWD/agent_audit.py" /usr/local/bin/agent-audit
# (or install via `pipx install .` / `uv tool install .`)

# 2. Install the skill so Codex and Antigravity know how to run and interpret it
mkdir -p ~/.codex/skills/agent-audit
cp skills/agent-audit/SKILL.md ~/.codex/skills/agent-audit/SKILL.md

mkdir -p ~/.gemini/antigravity-cli/skills/agent-audit
cp skills/agent-audit/SKILL.md ~/.gemini/antigravity-cli/skills/agent-audit/SKILL.md
```

---

## Quick CLI Usage

```bash
# Default 7-day report (English)
agent-audit

# Russian report
agent-audit --lang ru

# 30-day window or full history
agent-audit --days 30
agent-audit --all

# Filter by a single project
agent-audit --days 14 --project billing-api

# Machine-readable JSON summary
agent-audit --json

# Save report to file (created with 0600 permissions)
agent-audit --days 7 --output /tmp/agent-audit-latest.md
```

---

## Full Example Output (`agent-audit --days 7`)

```markdown
# Coding Agent Session Audit (7d window, 2026-11-02 to 2026-11-09 09:00 UTC)

## 1. Token Usage & Prompt Cache Efficiency

| Agent | Active Sessions | Model Steps | Total Input | Cached (%) | Uncached Input | Output |
|---|---:|---:|---:|---:|---:|---:|
| **Codex** | 84 | 3120 | 294.40M | 277.90M (94.4%) | **16.50M** | 2.64M (reas: 1.48M) |
| **Antigravity (`agy`)** | 41 | 1285 | 138.10M | 127.60M (92.4%) | **10.50M** | 980.0K |

### Breakdown by Project (Codex + Antigravity)
| Project | Steps | Total Input | Uncached Input | Output | Cache Hit % |
|---|---:|---:|---:|---:|---:|
| `web-studio` | 2890 | 285.00M | **17.20M** | 2.31M | 94.0% |
| `billing-api` | 980 | 104.50M | **6.40M** | 890.0K | 93.9% |
| `default` | 535 | 43.00M | **3.40M** | 420.0K | 92.1% |

### Phase Breakdown: Exploration vs Implementation KPI

| Agent | Active Sessions (with edits) | Exploration (before 1st edit) | Implementation & Verification | Pure Research Sessions | % Before 1st Edit (of all) | % Before 1st Edit (in edit sessions) | Avg Uncached / Edit Session |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Codex** | 84 (**31**) | **3.53M** (690 steps) | 9.09M (1790 steps) | 3.88M (640 steps) | **21.4%** | **28.0%** | **407.1K** |
| **Antigravity (`agy`)** | 41 (**10**) | **2.93M** (340 steps) | 5.97M (685 steps) | 1.60M (260 steps) | **27.9%** | **32.9%** | **889.7K** |

## 2. Cross-Session Repeated Lookups (Automation Candidates)

### `.env` & Credential Files Touched Across Sessions
- `/workspace/web-studio/backend/.env` — **11 sessions** (Codex: 8, Antigravity: 3)
- `/workspace/billing-api/.env.local` — **4 sessions** (Codex: 3, Antigravity: 1)

### Environment Variables / Secrets Queried in Commands
- `DATABASE_URL` — **11 sessions** (Codex: 8, Antigravity: 3)
- `SENTRY_AUTH_TOKEN` — **6 sessions** (Codex: 5, Antigravity: 1)
- `STRIPE_WEBHOOK_SECRET` — **4 sessions** (Codex: 4, Antigravity: 0)

### Most Frequently Read Application Files (Excluding Rules/Skills)
- `/workspace/web-studio/backend/app/services/generation.py` — **19 sessions** (Codex: 12, Antigravity: 7)
- `/workspace/web-studio/frontend/src/store/canvasStore.ts` — **14 sessions** (Codex: 9, Antigravity: 5)

### Repeated `rg` / `grep` Searches Across Sessions
- **9 sessions:** `rg -n "createCheckoutSession" backend/ frontend/`
- **6 sessions:** `rg -n "webhook_signature" backend/app/`

## 3. Tool Failures, Polling Loops & Workflow Bypasses

### Configured Tool Bypasses
- **Direct psql instead of safe DB wrapper:** 5 sessions (Codex: 4, Antigravity: 1)

### MCP Tool Calls & Error Rates (Codex)
| MCP Tool | Calls | Errors | Error % | Top Failure Reason |
|---|---:|---:|---:|---|
| `codegraph.find_symbol` | 184 | 6 | 3.3% | `status=error / empty confident_results` (6) |
| `codegraph.callers_of` | 92 | 19 | 20.7% | `input validation error (wrong arg name)` (14) |

### Codegraph Adoption in Antigravity (`agy`)
- **Codegraph calls:** **48 total calls** across **14 sessions** (34.1% of active agy sessions).
- **Comparison vs manual navigation:** `read_file`/`view_file` — **345 calls**, `grep_search` + shell `rg`/`grep` — **52 calls** (across 19 sessions).

| Codegraph Tool | Calls | Sessions | Share of Calls |
|---|---:|---:|---:|
| `mcp_codegraph_find_symbol` | 29 | 12 | 60.4% |
| `mcp_codegraph_semantic_search` | 11 | 6 | 22.9% |
| `mcp_codegraph_callers_of` | 8 | 5 | 16.7% |

### Top Failing Shell Commands (Codex)
- **14 failures** (out of 38 calls across 12 sessions): `pytest tests/api/test_webhooks.py -q`
- **7 failures** (out of 19 calls across 6 sessions): `gh pr checks <PR>`

### Consecutive Polling Loops (≥3 Identical Calls in a Row)
- **6 sessions with loop:** `gh pr checks <PR>`
- **3 sessions with loop:** `docker compose ps`

## 4. Context Bloat X-Ray (4-Lens Breakdown)

> [!NOTE] Metrics in Sections 2–4 (file reads, subagents, cache busts) are overlapping slices of total token spend (e.g., a subagent also reads files and can trigger a cache bust) and should not be summed mechanically.

- **Codex:** command outputs >30 KB — **12**, context compactions (`contextCompaction`) — **8** (across 5 sessions).
  Commands most frequently dumping >30 KB into Codex context:
  - `git diff` — **5x**
  - `python3 - <<'PY' (inline script)` — **4x**
- **Antigravity (`agy`):** compactions (`CHECKPOINT`) — **3** (across 2 sessions), truncations (`truncated_fields`) — `content`: **9**, `tool_calls`: **1**.
  File read discipline in Antigravity (`read_file`/`view_file`): line-bounded (`StartLine`/`EndLine`) — **284**, full unbounded reads — **61** (**17.7%** of all reads).

### 4.1. Intra-Session File Re-Reads (≥4x in a Single Session)
- **7x** (Codex `019a4f82-9c1`): `/workspace/web-studio/backend/app/services/generation.py`
- **5x** (agy `7b3e1190-4a2`): `/workspace/web-studio/frontend/src/store/canvasStore.ts`

### 4.2. Single-Step Context Jumps (`token spikes` ≥ +35K tokens)
- **+64.2K tokens** (total 118.4K, Codex `019a4f82-9c1`) after: `git diff`
- **+42.8K tokens** (total 91.0K, agy `7b3e1190-4a2`) after: `read_file(generation.py:FULL)`

### 4.3. Prefix Cache Busts (≥40K uncached input on a long context)
- Total cache bust events: **4** (uncached reload cost — Codex: **268.0K**, Antigravity: **94.5K**).
  - **+88.4K uncached** (context 112.0K, cached 14.0K, Codex `019a4f82-9c1`) on: `[after contextCompaction]`

### 4.4. Subagent Sprawl (Main Session vs Spawned Subagents)
- **Codex subagents:** 19 active child threads, total input **68.20M**, uncached **4.10M** (24.8% of Codex uncached input), output **610.0K**.
- **Antigravity subagents:** 8 active child threads, total input **24.50M**, uncached **1.90M** (18.1% of Antigravity uncached input), output **210.0K**.
  Parent sessions with highest subagent token spend:
  - Codex `019a5120-3b8` — **5 subagents**, subagent uncached **1.42M** (parent uncached: 380.0K), subagent output 190.0K — *Refactor payment provider adapters*

## 5. Top 10 Heaviest Sessions (by Uncached Input + Output)

| Agent | Date | Project | Session | Steps | Total Input | Uncached Input | Output | Compactions | Prompt / Topic |
|---|---|---|---|---:|---:|---:|---:|---:|---|
| Codex | 2026-11-08 | `web-studio` | `019a4f82-9c1` | 114 | 18.40M | **1.62M** | 145.0K | 3 | Fix canvas export race condition |
| agy | 2026-11-07 | `web-studio` | `7b3e1190-4a2` | 68 | 11.20M | **940.0K** | 88.0K | 1 | Add webhook retry diagnostics |
```

---

## How to Analyze & Optimize Workflows with Your Agent

`agent-audit` is designed for a **closed feedback loop inside your CLI**: you ask the agent to run the audit, review the top bottlenecks together, and let the agent update your helper scripts, MCP schemas, or `AGENTS.md` / `CLAUDE.md` rules.

### Scenario 1: Weekly Token & Bottleneck Review

Ask your agent in CLI (`codex` or `agy`):

> *"Run `agent-audit --days 7`, summarize where we burn the most uncached tokens, and propose 3 concrete fixes we can apply right now."*

What the agent does using `SKILL.md`:
1. Runs `agent-audit --days 7` and inspects **Uncached Input** (not just total cached input).
2. Checks **Section 2** (repeated `.env` reads and `rg` searches), **Section 3** (MCP errors and polling loops), and **Section 4** (token spikes, full-file reads, subagent sprawl).
3. Proposes targeted changes—for example:
   - Creating a read-only CLI wrapper so agents stop grepping `DATABASE_URL` and `SENTRY_AUTH_TOKEN` out of `.env` in every session;
   - Fixing an MCP tool schema where 20% of calls fail due to parameter name mismatches;
   - Adding a blocking CI check helper to stop `gh pr checks` polling loops.

### Scenario 2: Stopping `.env` Digging & Tool Bypasses

When **Section 2** shows agents reading `.env` across 10+ sessions to run ad-hoc database queries or API curls:

> *"Look at Section 2 of `agent-audit`. Agents keep reading `backend/.env` for `DATABASE_URL`. Build a small read-only CLI helper `/usr/local/bin/db-query` that loads `.env` internally, add a bypass rule to `~/.config/agent-audit/config.json`, and mention `/usr/local/bin/db-query` in `AGENTS.md`."*

On the next audit run, Section 2 `.env` touches and Section 3 bypasses drop toward zero.

### Scenario 3: Fixing Context Bloat & Cache Busts on a Specific Project

When a project feels slow or hits rate limits, ask your agent:

> *"Run `agent-audit --days 14 --project web-studio` and check Section 4. Which files or commands are causing token spikes and intra-session re-reads?"*

How to act on Section 4 numbers:
- **High `full unbounded reads %` or `read_file(..., :FULL)` spikes:** Add a rule to `AGENTS.md` requiring symbol lookup (`codegraph` / `rg -n`) followed by line-bounded `read_file` (`StartLine`/`EndLine`) for files over 400 lines.
- **`git diff` dumping `>30 KB`:** Instruct agents to run `git diff --stat` first and filter `git diff -- <path>`.
- **Subagent sprawl (`>30%` of uncached input):** Restrict subagent spawning in `AGENTS.md` to independent heavy tasks rather than spawning 5 subagents on the same files.

---

## Optional Configuration (`~/.config/agent-audit/config.json`)

Out of the box, `agent-audit` needs zero configuration: it scans `~/.codex` and `~/.gemini/antigravity-cli` and infers project names from working directories.

If you have multiple agent home directories (e.g. service accounts or scheduled runners), custom project groupings, or custom wrapper scripts you want agents to use instead of raw commands, copy [`config.example.json`](config.example.json) to `~/.config/agent-audit/config.json`:

```json
{
  "codex_homes": [
    "~/.codex"
  ],
  "agy_roots": [
    {
      "label": "default",
      "path": "~/.gemini/antigravity-cli"
    }
  ],
  "projects": {
    "my-webapp": ["my-webapp", "webapp-frontend", "webapp-backend"],
    "infra": ["terraform", "ansible", "k8s"]
  },
  "bypass_rules": [
    {
      "name": "Direct psql instead of safe DB wrapper",
      "pattern": "\\bpsql\\b|asyncpg\\.connect|psycopg2",
      "unless": "db-safe-query"
    }
  ]
}
```

---

## Security & Privacy

- **Strictly read-only:** SQLite databases are opened with `?mode=ro` (`uri=True`); JSONL transcripts are never modified.
- **Zero secret leakage:** `.env` diagnostics record only file paths and uppercase variable names (`DATABASE_URL`, `STRIPE_SECRET_KEY`), never their values. Long token-like strings in `rg` queries or command signatures are redacted (`<REDACTED>`).
- **Private report files:** When `--output <path>` is passed, the output file is created with `0600` permissions.

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
