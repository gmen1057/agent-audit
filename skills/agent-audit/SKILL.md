---
name: agent-audit
description: Audit coding agent sessions (Codex and Antigravity) using the `agent-audit` CLI. Use whenever asked to analyze token usage, prompt cache hit rate, uncached token spend, tool call error rates, repeated manual file/.env lookups, polling loops, subagent sprawl, or context bloat across agent sessions ("audit agent sessions", "where are my tokens going", "why is prompt cache dropping", "проверь расход токенов", "на что агенты тратят токены", "где косяки в сессиях", "аудит сессий агентов").
---

# Coding Agent Session Auditor (`agent-audit`)

`agent-audit` is a strictly read-only, zero-dependency CLI analyzer for **OpenAI Codex CLI** (`~/.codex`) and **Google Antigravity CLI (`agy`)** (`~/.gemini/antigravity-cli`) sessions. It never prints secret values from `.env` files or command outputs and applies verified deduplication rules for token accounting.

## Quick Start

```bash
# Default 7-day audit across all detected Codex & Antigravity sessions (English)
agent-audit

# Russian report (--lang ru)
agent-audit --lang ru

# 30-day window or full history
agent-audit --days 30
agent-audit --all

# Filter by a specific project slug
agent-audit --days 14 --project my-webapp

# Machine-readable JSON summary
agent-audit --json

# Save report to a file (permissions 0600)
agent-audit --days 7 --output /tmp/agent-audit-latest.md
```

## What the Report Covers

1. **Token Usage & Prompt Cache Efficiency** (by agent and by project):
   - Active sessions and model steps;
   - Total input tokens, cache hit rate (`%`), **uncached input tokens** (actual quota/cost driver), and output tokens (including reasoning tokens).
2. **Cross-Session Repeated Lookups (Automation Candidates):**
   - Which `.env` and credential files agents manually inspected across multiple sessions, and which variable names (`DATABASE_URL`, `OPENAI_API_KEY`, `SENTRY_DSN`, etc.) they grepped for;
   - Which application files (excluding `AGENTS.md`/`SKILL.md`/rules) are read across the highest number of sessions;
   - Which `rg` / `grep` queries are repeated from session to session.
3. **Tool Failures, Polling Loops & Workflow Bypasses:**
   - Custom tool-bypass checks (configured in `~/.config/agent-audit/config.json`);
   - MCP tool call counts, failure rates (`%`), and top failure reasons;
   - Top failing shell commands and consecutive polling loops (`≥3` identical commands in a row within a single session, e.g. manual `gh pr checks` polling).
4. **Context Bloat X-Ray (4-Lens Breakdown):**
   - **Command outputs `>30 KB`** and context compaction counts (`contextCompaction` in Codex, `CHECKPOINT` in Antigravity);
   - **File read discipline** in Antigravity (`StartLine`/`EndLine` bounded reads vs full unbounded file reads) and `truncated_fields` counts;
   - **4.1 Intra-session file re-reads** (`≥4x` reads of the same file within a single session);
   - **4.2 Single-step token spikes** (`≥ +35K` input token jump in a single turn, with the exact tool/command that caused it);
   - **4.3 Prefix cache busts** (`≥40K` uncached input tokens when cached prefix drops below 50% on a `≥50K` context);
   - **4.4 Subagent sprawl** (main session vs spawned child subagents token share).
5. **Top 10 Heaviest Sessions** (ranked by uncached input + output) with date, project, step count, compactions, and prompt topic.

## Verified Token Accounting Rules

- **Codex (`~/.codex`)**:
  - Thread metadata from `state_5.sqlite` (`threads`);
  - Command executions, MCP calls, errors, and `contextCompaction` from `thread_history_1.sqlite` (`thread_items`, deduplicated by `(thread_id, item_id)`);
  - Token usage from `sessions/**/*.jsonl` deduplicated by unique `token_usage_record.response_id` (fallback for older logs without `token_usage_record`: non-negative deltas of `total_token_usage` in `token_count`). In Codex, `input_tokens` already includes `cached_input_tokens` (`uncached = input_tokens - cached_input_tokens`), and `output_tokens` already includes `reasoning_output_tokens`.
- **Antigravity (`~/.gemini/antigravity-cli`)**:
  - Step logs from `brain/*/.system_generated/logs/transcript.jsonl`, `PLANNER_RESPONSE` steps deduplicated by `(session_id, step_index, created_at)`;
  - Unlike Codex, `input_tokens` in Antigravity's `transcript.jsonl` represents **uncached** input tokens, while `cache_read_tokens` is reported separately (`total_input = input_tokens + cache_read_tokens`).

## Agent Playbook: How to Analyze & Turn Findings into Fixes

When the user asks you to run `agent-audit` and analyze sessions:

1. **Run the CLI in the user's preferred language** (`agent-audit --days 7` or `agent-audit --days 7 --lang ru`).
2. **Focus on Uncached Input + Output first:** High total input with `95%+` cache hit is cheap; high **Uncached Input** is what actually drains quota and money.
3. **Translate each noisy metric into a concrete, actionable fix:**
   - **Repeated `.env` / secret lookups (Section 2, `≥5` sessions):** Propose a small read-only CLI wrapper (e.g. in `/usr/local/bin/`) that loads `.env` internally without printing keys into bash, and add a `bypass_rules` entry in `~/.config/agent-audit/config.json`.
   - **Repeated `rg` searches or hot files across sessions (Section 2):** Add the symbol/route locations to the project's entrypoint map (`MAP.md`, `FLOWS.md`, or `AGENTS.md`) so agents stop searching from scratch.
   - **High MCP error rate (Section 3, `>10%`):** Check the top failure reason. If it is `input validation error (wrong arg name)`, add parameter aliases on the MCP server or document the exact parameter in the skill.
   - **Consecutive polling loops (Section 3, `≥3` identical calls):** Replace manual `gh pr checks` or `docker ps` polling with a single blocking wait helper script.
   - **Command outputs `>30 KB`, token spikes & full-file reads (Section 4):** Recommend `git diff --stat` / path-scoped diffs and line-bounded `read_file` (`StartLine`/`EndLine`) for files over 400 lines.
   - **High subagent uncached share (Section 4.4, `>30%`):** Each spawned subagent warms up its own prompt cache. Recommend restricting subagents in `AGENTS.md` to heavy independent tasks rather than spawning multiple helpers on the same files.
