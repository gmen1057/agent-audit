#!/usr/bin/env python3
"""agent-audit: CLI & Agent Skill for auditing Codex and Antigravity (agy) coding sessions.

Strictly read-only. Never prints secret values from .env or command outputs.
Zero external dependencies (Python 3.10+ standard library only).

Key accounting rules:
- Codex tokens: deduplicated by unique token_usage_record.response_id
  (fallback for older logs: non-negative deltas of total_token_usage).
  input_tokens in Codex already includes cached_input_tokens;
  output_tokens already includes reasoning_output_tokens.
- Antigravity (agy) tokens: deduplicated by (session_id, step_index, created_at).
  input_tokens in transcript.jsonl is uncached input; cache_read_tokens is reported separately.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

RULE_DOC_PATTERNS = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "SKILL.md",
    "/docs/learned/",
    "/docs/agent-rules/",
    "/.agent/rules/",
    "/.agents/skills/",
    "/.claude/rules/",
    "/.claude/skills/",
    "/.codex/memories/",
    "/.codex/skills/",
)

GENERIC_PATH_PARTS = {
    "",
    "root",
    "home",
    "users",
    "opt",
    "var",
    "tmp",
    "wt",
    "worktrees",
    "private",
    "src",
    "repos",
    "code",
    "projects",
    "workspace",
    "workspaces",
    "dev",
}


def load_config(config_path: str | None) -> dict[str, Any]:
    paths: list[Path] = []
    if config_path:
        paths.append(Path(config_path).expanduser())
    env_cfg = os.environ.get("AGENT_AUDIT_CONFIG")
    if env_cfg:
        paths.append(Path(env_cfg).expanduser())
    paths.append(Path.home() / ".config" / "agent-audit" / "config.json")

    for p in paths:
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def discover_codex_homes(cli_homes: list[str] | None, cfg: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    if cli_homes:
        candidates.extend(Path(p).expanduser() for p in cli_homes)
    env_homes = os.environ.get("AGENT_AUDIT_CODEX_HOMES") or os.environ.get("CODEX_HOME")
    if env_homes:
        for part in env_homes.split(os.pathsep):
            if part.strip():
                candidates.append(Path(part.strip()).expanduser())
    for p in cfg.get("codex_homes", []):
        candidates.append(Path(p).expanduser())

    if not candidates:
        home = Path.home()
        candidates.append(home / ".codex")
        for extra in home.glob(".config/*/codex*/home/.codex"):
            candidates.append(extra)

    seen: set[Path] = set()
    result: list[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            resolved = c
        if resolved not in seen and c.exists():
            seen.add(resolved)
            result.append(c)
    return result


def discover_agy_roots(cli_roots: list[str] | None, cfg: dict[str, Any]) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    if cli_roots:
        for item in cli_roots:
            if "=" in item:
                lbl, p = item.split("=", 1)
                candidates.append((lbl.strip(), Path(p.strip()).expanduser()))
            else:
                p = Path(item.strip()).expanduser()
                candidates.append((p.name, p))

    env_roots = os.environ.get("AGENT_AUDIT_AGY_ROOTS") or os.environ.get("AGY_HOME")
    if env_roots:
        for part in env_roots.split(os.pathsep):
            if part.strip():
                p = Path(part.strip()).expanduser()
                candidates.append((p.name, p))

    for entry in cfg.get("agy_roots", []):
        if isinstance(entry, dict) and "path" in entry:
            p = Path(entry["path"]).expanduser()
            candidates.append((str(entry.get("label") or p.name), p))
        elif isinstance(entry, str):
            p = Path(entry).expanduser()
            candidates.append((p.name, p))

    if not candidates:
        home = Path.home()
        candidates.append(("default", home / ".gemini" / "antigravity-cli"))

    seen: set[Path] = set()
    result: list[tuple[str, Path]] = []
    for lbl, c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            resolved = c
        if resolved not in seen and c.exists():
            seen.add(resolved)
            result.append((lbl, c))
    return result


def classify_project(text: str, custom_rules: dict[str, list[str]] | None = None) -> str:
    s = (text or "").strip()
    s_low = s.lower()

    if custom_rules:
        for proj_name, keywords in custom_rules.items():
            if any(k.lower() in s_low for k in keywords):
                return proj_name

    # Extract paths from text and infer project name from directory structure
    paths = re.findall(r"(/[A-Za-z0-9_./-]+)", s)
    home_str = str(Path.home())

    for raw_p in paths:
        if raw_p == home_str or raw_p.startswith(home_str + "/."):
            continue
        parts = [pt for pt in raw_p.split("/") if pt and not pt.startswith(".")]
        if not parts:
            continue
        # Drop username if under /Users/<user>/... or /home/<user>/...
        if len(parts) >= 2 and parts[0].lower() in ("users", "home"):
            parts = parts[2:]
        filtered = [pt for pt in parts if pt.lower() not in GENERIC_PATH_PARTS]
        if filtered:
            candidate = filtered[0]
            # Strip common worktree suffixes like -wt, -fix, etc. only if clearly a path dir
            return candidate[:40]

    return "default"


def is_rule_or_skill_path(p: str) -> bool:
    return any(pat in p for pat in RULE_DOC_PATTERNS)


def strip_bash_wrapper(cmd: str) -> str:
    c = (cmd or "").strip()
    m = re.match(r"^(?:/bin/bash|/usr/bin/env\s+bash|bash|/bin/zsh|zsh)\s+-[a-zA-Z]*c\s+['\"](.*)['\"]$", c, re.S)
    if m:
        c = m.group(1).strip()
    return c


def normalize_cmd_signature(cmd: str) -> str:
    c = strip_bash_wrapper(cmd)
    lines = [ln.strip() for ln in c.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return "(empty)"
    first = lines[0]
    if re.match(r"^(?:python3?|/[^\s]+/python3?)\s+-\s*<<", first):
        return "python3 - <<'PY' (inline script)"
    if re.match(r"^(?:python3?|/[^\s]+/python3?)\s+-c\b", first):
        return "python3 -c (inline one-liner)"
    if re.match(r"^node\s+(?:-\s*)?<<", first) or re.match(r"^node\s+-e\b", first):
        return "node inline script"
    if first.startswith("gh pr checks"):
        return "gh pr checks <PR>"
    # Redact potential inline tokens in signatures
    first = re.sub(r"[A-Za-z0-9_-]{32,}", "<REDACTED>", first)
    return first[:90]


def extract_env_touches(text: str) -> tuple[list[str], list[str]]:
    """Return (env_files, env_var_names) referenced in .env reads/greps (excluding dummy test exports)."""
    env_files = re.findall(
        r"((?:[A-Za-z0-9_./~-]*/)?\.env(?:\.[A-Za-z0-9_.-]+)?)\b",
        text,
    )
    cleaned_files = [
        ef for ef in env_files
        if not ef.endswith((".env.example", ".env.sample", ".env.template"))
    ]
    env_vars: list[str] = []
    if cleaned_files or any(w in text for w in ("dotenv", "source ", "grep ", "rg ", "curl ", "psql ", "cat ")):
        for v in re.findall(
            r"\b([A-Z][A-Z0-9_]{2,}_(?:URL|KEY|TOKEN|SECRET|DSN|PASSWORD|HOST|API))\b",
            text,
        ):
            if re.search(rf"\b{v}=(?:test|dummy|fake|review-test|123|example)", text, re.I):
                continue
            env_vars.append(v)
    return cleaned_files, env_vars


def extract_rg_patterns(cmd: str) -> list[str]:
    c = strip_bash_wrapper(cmd)
    results = []
    for m in re.finditer(r"\b(?:rg|grep)\b\s+([^\n|;&]+)", c):
        seg = m.group(1).strip()
        seg = re.sub(r"[A-Za-z0-9_-]{28,}", "<TOKEN>", seg)
        results.append(seg[:80])
    return results


def extract_codex_read_files(cmd: str) -> list[str]:
    """Extract files being read in shell commands (sed, nl, cat, head, tail, bat)."""
    c = strip_bash_wrapper(cmd)
    if not any(w in c for w in ("sed ", "nl ", "cat ", "head ", "tail ", "bat ")):
        return []
    paths = re.findall(
        r"((?:/[A-Za-z0-9_./-]+|[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)\.[A-Za-z0-9]+)",
        c,
    )
    return [p for p in paths if not p.endswith((".pyc", ".sqlite", ".lock", ".json.gz"))]


def check_bypasses(cmd_lower: str, custom_bypasses: list[dict[str, str]] | None) -> list[str]:
    triggered: list[str] = []
    if custom_bypasses:
        for rule in custom_bypasses:
            name = rule.get("name") or "Custom bypass"
            pattern = rule.get("pattern") or ""
            unless = rule.get("unless") or ""
            if pattern and re.search(pattern, cmd_lower):
                if not unless or unless.lower() not in cmd_lower:
                    triggered.append(name)
    return triggered


def parse_iso_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_subagent_parent(source_val: Any) -> tuple[str | None, str | None]:
    """Return (parent_thread_id, agent_label) if Codex thread is a spawned subagent."""
    if not source_val:
        return None, None
    obj = source_val
    if isinstance(source_val, str) and source_val.startswith("{"):
        try:
            obj = json.loads(source_val)
        except Exception:
            return None, None
    if isinstance(obj, dict):
        sub = obj.get("subagent") or {}
        ts = sub.get("thread_spawn") or {}
        parent_id = ts.get("parent_thread_id")
        label = ts.get("agent_path") or ts.get("agent_nickname") or "subagent"
        if parent_id:
            return str(parent_id), str(label)
    return None, None


def collect_codex(
    codex_homes: list[Path],
    start_dt: datetime,
    project_filter: str | None,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    custom_proj_rules = cfg.get("projects")
    custom_bypasses = cfg.get("bypass_rules")

    threads_meta: dict[str, dict[str, Any]] = {}
    subagent_parent: dict[str, str] = {}
    subagent_label: dict[str, str] = {}

    for home in codex_homes:
        db = home / "state_5.sqlite"
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for row in conn.execute(
                "SELECT id, cwd, model, reasoning_effort, tokens_used, created_at, updated_at, title, first_user_message, source FROM threads"
            ):
                tid = row[0]
                cwd = row[1] or str(Path.home())
                proj = classify_project(cwd + " " + (row[7] or "") + " " + (row[8] or ""), custom_proj_rules)
                parent_id, ag_label = parse_subagent_parent(row[9])
                if parent_id:
                    subagent_parent[tid] = parent_id
                    subagent_label[tid] = ag_label or "subagent"
                threads_meta[tid] = {
                    "id": tid,
                    "cwd": cwd,
                    "project": proj,
                    "model": row[2] or "unknown",
                    "effort": row[3] or "unknown",
                    "tokens_used_db": row[4] or 0,
                    "created_at": row[5] or 0,
                    "updated_at": row[6] or 0,
                    "title": (row[7] or row[8] or "")[:100].replace("\n", " "),
                    "parent_id": parent_id,
                }
        except Exception:
            pass

    seen_resp_ids: set[str] = set()
    seen_rollout_files: set[str] = set()
    token_totals = Counter()
    subagent_token_totals = Counter()
    by_project_tokens: dict[str, Counter] = defaultdict(Counter)
    by_session_tokens: dict[str, Counter] = defaultdict(Counter)
    session_models: dict[str, str] = {}
    session_cwds: dict[str, str] = {}
    session_titles: dict[str, str] = {}
    session_dates: dict[str, str] = {}
    compactions_by_session: Counter = Counter()

    token_spikes: list[dict[str, Any]] = []
    cache_busts: list[dict[str, Any]] = []
    cache_bust_tokens_by_session: Counter = Counter()

    start_ts = start_dt.timestamp()
    for home in codex_homes:
        sess_dir = home / "sessions"
        if not sess_dir.exists():
            continue
        for path in sess_dir.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < start_ts:
                    continue
            except OSError:
                continue
            if path.name in seen_rollout_files:
                continue
            seen_rollout_files.add(path.name)

            sid = None
            cwd = str(Path.home())
            model = "unknown"
            has_usage_records = False
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if '"type":"token_usage_record"' in line or '"type": "token_usage_record"' in line:
                            has_usage_records = True
                            break
            except Exception:
                continue

            prev_total: dict[str, int] = {}
            prev_step_input_by_sid: dict[str, int] = {}
            last_action_by_sid: dict[str, str] = {}

            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        typ = obj.get("type")
                        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                        ts_str = obj.get("timestamp") or ""
                        dt = parse_iso_ts(ts_str)

                        if typ == "session_meta":
                            sid = p.get("id") or sid
                            cwd = p.get("cwd") or cwd
                            if sid:
                                session_cwds[sid] = cwd
                                p_id, ag_lbl = parse_subagent_parent(p.get("source"))
                                if p_id and sid not in subagent_parent:
                                    subagent_parent[sid] = p_id
                                    subagent_label[sid] = ag_lbl or "subagent"
                        elif typ == "turn_context":
                            if p.get("model"):
                                model = p["model"]
                            if p.get("cwd") and sid:
                                cwd = p["cwd"]
                                session_cwds[sid] = cwd
                        elif typ == "response_item" and sid:
                            ptype = p.get("type")
                            if ptype in ("function_call", "custom_tool_call"):
                                name = p.get("name") or "tool"
                                args_raw = str(p.get("arguments") or p.get("input") or "")
                                if name == "exec_command":
                                    try:
                                        a_obj = json.loads(args_raw)
                                        last_action_by_sid[sid] = normalize_cmd_signature(a_obj.get("cmd") or "")
                                    except Exception:
                                        last_action_by_sid[sid] = "exec_command"
                                else:
                                    last_action_by_sid[sid] = f"{name}({args_raw[:60]})"

                        if dt and dt < start_dt:
                            if not has_usage_records and typ == "event_msg" and p.get("type") == "token_count":
                                tot = (p.get("info") or {}).get("total_token_usage")
                                if isinstance(tot, dict):
                                    prev_total = tot
                            continue

                        if typ == "compacted" and sid:
                            compactions_by_session[sid] += 1
                            last_action_by_sid[sid] = "[after contextCompaction]"

                        if typ == "event_msg" and p.get("type") == "user_message" and sid and sid not in session_titles:
                            session_titles[sid] = (p.get("message") or "")[:90].replace("\n", " ")

                        if typ == "token_usage_record":
                            rid = p.get("response_id")
                            if not rid or rid in seen_resp_ids:
                                continue
                            seen_resp_ids.add(rid)
                            t_sid = p.get("thread_id") or sid or p.get("session_id") or "unknown"
                            parent_sid = p.get("session_id")
                            if parent_sid and parent_sid != t_sid and t_sid not in subagent_parent:
                                subagent_parent[t_sid] = parent_sid
                            u = p.get("usage") or {}
                            meta = threads_meta.get(t_sid, {})
                            proj = meta.get("project") or classify_project(session_cwds.get(t_sid, cwd), custom_proj_rules)
                            if project_filter and proj != project_filter:
                                continue
                            inp = u.get("input_tokens", 0)
                            cac = u.get("cached_input_tokens", 0)
                            out = u.get("output_tokens", 0)
                            reas = u.get("reasoning_output_tokens", 0)
                            unc = max(0, inp - cac)

                            prev_inp = prev_step_input_by_sid.get(t_sid, 0)
                            prev_step_input_by_sid[t_sid] = inp
                            act = last_action_by_sid.get(t_sid, "(start/reply)")

                            if prev_inp > 0 and (inp - prev_inp) >= 35_000:
                                token_spikes.append({
                                    "jump_tokens": inp - prev_inp,
                                    "total_input": inp,
                                    "uncached": unc,
                                    "agent": "Codex",
                                    "session_id": t_sid,
                                    "action": act,
                                })
                            if prev_inp >= 50_000 and cac < prev_inp * 0.5 and unc >= 40_000:
                                cache_busts.append({
                                    "uncached_loss": unc,
                                    "prev_input": prev_inp,
                                    "new_input": inp,
                                    "cached": cac,
                                    "agent": "Codex",
                                    "session_id": t_sid,
                                    "action": act,
                                })
                                cache_bust_tokens_by_session[t_sid] += unc

                            delta = {
                                "input_tokens": inp,
                                "cached_input_tokens": cac,
                                "output_tokens": out,
                                "reasoning_output_tokens": reas,
                                "steps": 1,
                            }
                            token_totals.update(delta)
                            if t_sid in subagent_parent:
                                subagent_token_totals.update(delta)
                            by_project_tokens[proj].update(delta)
                            by_session_tokens[t_sid].update(delta)
                            session_models[t_sid] = meta.get("model") or model
                            session_dates[t_sid] = ts_str[:10]
                        elif not has_usage_records and typ == "event_msg" and p.get("type") == "token_count":
                            tot = (p.get("info") or {}).get("total_token_usage")
                            if not isinstance(tot, dict) or tot == prev_total:
                                continue
                            t_sid = sid or "unknown"
                            keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
                            d_vals = {k: tot.get(k, 0) - prev_total.get(k, 0) for k in keys}
                            if any(v < 0 for v in d_vals.values()):
                                d_vals = {k: tot.get(k, 0) for k in keys}
                            prev_total = tot
                            meta = threads_meta.get(t_sid, {})
                            proj = meta.get("project") or classify_project(session_cwds.get(t_sid, cwd), custom_proj_rules)
                            if project_filter and proj != project_filter:
                                continue
                            d_vals["steps"] = 1
                            token_totals.update(d_vals)
                            if t_sid in subagent_parent:
                                subagent_token_totals.update(d_vals)
                            by_project_tokens[proj].update(d_vals)
                            by_session_tokens[t_sid].update(d_vals)
                            session_models[t_sid] = meta.get("model") or model
                            session_dates[t_sid] = ts_str[:10]
            except Exception:
                continue

    seen_items: set[tuple[str, str]] = set()
    cmd_sessions: dict[str, set[str]] = defaultdict(set)
    cmd_counts: Counter = Counter()
    cmd_fails: Counter = Counter()
    mcp_counts: Counter = Counter()
    mcp_fails: Counter = Counter()
    mcp_fail_reasons: dict[str, Counter] = defaultdict(Counter)
    env_file_sessions: dict[str, set[str]] = defaultdict(set)
    env_var_sessions: dict[str, set[str]] = defaultdict(set)
    file_touch_sessions: dict[str, set[str]] = defaultdict(set)
    rg_pattern_sessions: dict[str, set[str]] = defaultdict(set)
    big_outputs: list[dict[str, Any]] = []
    big_output_by_sig: Counter = Counter()
    bypass_counts: dict[str, set[str]] = defaultdict(set)
    polling_loops: Counter = Counter()
    intra_session_reads: Counter = Counter()

    start_ms = int(start_dt.timestamp() * 1000)

    for home in codex_homes:
        db = home / "thread_history_1.sqlite"
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute(
                """
                SELECT thread_id, item_id, item_type, created_at_ms, item_json
                FROM thread_items
                WHERE created_at_ms >= ?
                  AND item_type IN ('commandExecution', 'mcpToolCall', 'contextCompaction')
                ORDER BY thread_id, rollout_ordinal
                """,
                (start_ms,),
            ).fetchall()
        except Exception:
            continue

        last_sig_by_thread: dict[str, str] = {}
        streak_by_thread: dict[str, int] = defaultdict(int)

        for tid, item_id, itype, created_ms, ijson in rows:
            if (tid, item_id) in seen_items:
                continue
            seen_items.add((tid, item_id))
            meta = threads_meta.get(tid, {})
            proj = meta.get("project") or classify_project(meta.get("cwd", ""), custom_proj_rules)
            if project_filter and proj != project_filter:
                continue

            if itype == "contextCompaction":
                compactions_by_session[tid] += 1
                continue

            try:
                d = json.loads(ijson)
            except Exception:
                continue

            if itype == "commandExecution":
                raw_cmd = d.get("command") or ""
                sig = normalize_cmd_signature(raw_cmd)
                cmd_counts[sig] += 1
                cmd_sessions[sig].add(tid)

                if last_sig_by_thread.get(tid) == sig:
                    streak_by_thread[tid] += 1
                    if streak_by_thread[tid] == 3:
                        polling_loops[sig] += 1
                else:
                    last_sig_by_thread[tid] = sig
                    streak_by_thread[tid] = 1

                exit_code = d.get("exitCode")
                status = d.get("status")
                if (exit_code not in (0, None)) or status == "failed":
                    cmd_fails[sig] += 1

                out_str = d.get("aggregatedOutput") or ""
                if len(out_str) > 30000:
                    big_output_by_sig[sig] += 1
                    big_outputs.append({
                        "size_kb": round(len(out_str) / 1024, 1),
                        "sig": sig,
                        "thread_id": tid,
                        "project": proj,
                    })

                efiles, evars = extract_env_touches(raw_cmd)
                for ef in efiles:
                    env_file_sessions[ef].add(tid)
                for ev in evars:
                    env_var_sessions[ev].add(tid)

                c_lower = raw_cmd.lower()
                for b_name in check_bypasses(c_lower, custom_bypasses):
                    bypass_counts[b_name].add(tid)

                for rg_pat in extract_rg_patterns(raw_cmd):
                    rg_pattern_sessions[rg_pat].add(tid)

                for r_path in set(extract_codex_read_files(raw_cmd)):
                    intra_session_reads[(tid, r_path)] += 1
                    if not is_rule_or_skill_path(r_path):
                        file_touch_sessions[r_path].add(tid)

            elif itype == "mcpToolCall":
                srv = d.get("server") or "mcp"
                tl = d.get("tool") or "unknown"
                key = f"{srv}.{tl}"
                mcp_counts[key] += 1
                if d.get("status") == "failed" or d.get("error"):
                    mcp_fails[key] += 1
                    err_txt = str(d.get("error") or d.get("result") or "")
                    if "confident_results" in err_txt:
                        mcp_fail_reasons[key]["status=error / empty confident_results"] += 1
                    elif "Unrecognized key" in err_txt or "Invalid arguments" in err_txt:
                        mcp_fail_reasons[key]["input validation error (wrong arg name)"] += 1
                    else:
                        mcp_fail_reasons[key][err_txt[:80].replace("\n", " ")] += 1

    return {
        "threads_meta": threads_meta,
        "subagent_parent": subagent_parent,
        "subagent_label": subagent_label,
        "token_totals": token_totals,
        "subagent_token_totals": subagent_token_totals,
        "by_project_tokens": by_project_tokens,
        "by_session_tokens": by_session_tokens,
        "session_models": session_models,
        "session_titles": session_titles,
        "session_dates": session_dates,
        "compactions_by_session": compactions_by_session,
        "cmd_sessions": cmd_sessions,
        "cmd_counts": cmd_counts,
        "cmd_fails": cmd_fails,
        "mcp_counts": mcp_counts,
        "mcp_fails": mcp_fails,
        "mcp_fail_reasons": mcp_fail_reasons,
        "env_file_sessions": env_file_sessions,
        "env_var_sessions": env_var_sessions,
        "file_touch_sessions": file_touch_sessions,
        "rg_pattern_sessions": rg_pattern_sessions,
        "big_outputs": big_outputs,
        "big_output_by_sig": big_output_by_sig,
        "bypass_counts": bypass_counts,
        "polling_loops": polling_loops,
        "intra_session_reads": intra_session_reads,
        "token_spikes": token_spikes,
        "cache_busts": cache_busts,
        "cache_bust_tokens_by_session": cache_bust_tokens_by_session,
    }


def collect_antigravity(
    agy_roots: list[tuple[str, Path]],
    start_dt: datetime,
    project_filter: str | None,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    custom_proj_rules = cfg.get("projects")
    custom_bypasses = cfg.get("bypass_rules")

    seen_steps: set[tuple[str, Any, str]] = set()
    token_totals = Counter()
    subagent_token_totals = Counter()
    by_project_tokens: dict[str, Counter] = defaultdict(Counter)
    by_root_tokens: dict[str, Counter] = defaultdict(Counter)
    by_session_tokens: dict[str, Counter] = defaultdict(Counter)
    session_titles: dict[str, str] = {}
    session_dates: dict[str, str] = {}
    session_projects: dict[str, str] = {}
    compactions_by_session: Counter = Counter()
    subagent_parent: dict[str, str] = {}

    tool_counts: Counter = Counter()
    tool_truncations: Counter = Counter()
    truncated_fields_count: Counter = Counter()
    error_steps: Counter = Counter()
    env_file_sessions: dict[str, set[str]] = defaultdict(set)
    env_var_sessions: dict[str, set[str]] = defaultdict(set)
    file_touch_sessions: dict[str, set[str]] = defaultdict(set)
    rg_pattern_sessions: dict[str, set[str]] = defaultdict(set)
    bypass_counts: dict[str, set[str]] = defaultdict(set)
    cmd_sessions: dict[str, set[str]] = defaultdict(set)
    intra_session_reads: Counter = Counter()
    read_bounded_counts: Counter = Counter()
    token_spikes: list[dict[str, Any]] = []
    cache_busts: list[dict[str, Any]] = []
    cache_bust_tokens_by_session: Counter = Counter()

    start_ts = start_dt.timestamp()

    transcript_paths: list[tuple[str, Path]] = []
    for root_label, root_path in agy_roots:
        if not root_path.exists():
            continue
        for path in root_path.glob("brain/*/.system_generated/logs/transcript.jsonl"):
            try:
                if path.stat().st_mtime < start_ts:
                    continue
            except OSError:
                continue
            transcript_paths.append((root_label, path))
            parent_sid = path.parts[-4]
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "Created the following subagent" in line and "conversationId" in line:
                            for m in re.finditer(r'conversationId\\?"\s*:\s*\\?"([0-9a-f-]{36})', line):
                                subagent_parent[m.group(1)] = parent_sid
            except Exception:
                continue

    for root_label, path in transcript_paths:
        sid = path.parts[-4]
        proj_hint = "" if root_label in ("default", "root", "antigravity-cli") else root_label
        first_user_text = ""
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f):
                    if i > 30:
                        break
                    if "/" in line:
                        proj_hint += " " + line[:500]
                    if '"USER_INPUT"' in line and not first_user_text:
                        try:
                            o = json.loads(line)
                            first_user_text = str(o.get("content") or "")[:120].replace("\n", " ")
                        except Exception:
                            pass
        except Exception:
            continue

        proj = classify_project(proj_hint + " " + first_user_text, custom_proj_rules)
        if project_filter and proj != project_filter:
            continue
        session_projects[sid] = proj
        if first_user_text:
            session_titles[sid] = re.sub(r"</?USER_REQUEST>", "", first_user_text).strip()[:90]

        prev_inp = 0
        last_action = "(start/reply)"

        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                last_tools_in_session: list[str] = []
                for line in f:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    ts_str = ev.get("created_at") or ""
                    dt = parse_iso_ts(ts_str)
                    if dt and dt < start_dt:
                        continue

                    if ev.get("type") == "CHECKPOINT":
                        compactions_by_session[sid] += 1
                        last_action = "[after CHECKPOINT compaction]"

                    if ev.get("truncated_fields"):
                        for tf in ev["truncated_fields"]:
                            truncated_fields_count[tf] += 1
                        for lt in set(last_tools_in_session):
                            tool_truncations[lt] += 1
                        last_tools_in_session = []

                    if ev.get("status") == "ERROR":
                        msg = str(ev.get("content") or "ERROR")[:80].replace("\n", " ")
                        error_steps[msg] += 1

                    if ev.get("type") == "PLANNER_RESPONSE":
                        key = (sid, ev.get("step_index"), ts_str)
                        if key in seen_steps:
                            continue
                        seen_steps.add(key)
                        unc_in = ev.get("input_tokens", 0)
                        cac_in = ev.get("cache_read_tokens", 0)
                        tot_in = unc_in + cac_in
                        out_tok = ev.get("output_tokens", 0)

                        if prev_inp > 0 and (tot_in - prev_inp) >= 35_000:
                            token_spikes.append({
                                "jump_tokens": tot_in - prev_inp,
                                "total_input": tot_in,
                                "uncached": unc_in,
                                "agent": "agy",
                                "session_id": sid,
                                "action": last_action,
                            })
                        if prev_inp >= 50_000 and cac_in < prev_inp * 0.5 and unc_in >= 40_000:
                            cache_busts.append({
                                "uncached_loss": unc_in,
                                "prev_input": prev_inp,
                                "new_input": tot_in,
                                "cached": cac_in,
                                "agent": "agy",
                                "session_id": sid,
                                "action": last_action,
                            })
                            cache_bust_tokens_by_session[sid] += unc_in
                        prev_inp = tot_in

                        d_tok = {
                            "input_tokens": tot_in,
                            "cached_input_tokens": cac_in,
                            "output_tokens": out_tok,
                            "steps": 1,
                        }
                        token_totals.update(d_tok)
                        if sid in subagent_parent:
                            subagent_token_totals.update(d_tok)
                        by_project_tokens[proj].update(d_tok)
                        by_root_tokens[root_label].update(d_tok)
                        by_session_tokens[sid].update(d_tok)
                        if ts_str:
                            session_dates[sid] = ts_str[:10]

                        last_tools_in_session = []
                        step_actions: list[str] = []
                        for tc in ev.get("tool_calls") or []:
                            tname = tc.get("name") or tc.get("tool_name") or "unknown"
                            tool_counts[tname] += 1
                            last_tools_in_session.append(tname)
                            args = tc.get("arguments") or tc.get("args") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {}
                            if not isinstance(args, dict):
                                continue

                            target_path = str(args.get("Path") or args.get("AbsolutePath") or args.get("TargetFile") or "")
                            cmd_line = str(args.get("CommandLine") or args.get("Command") or "")
                            combined_arg = target_path + "\n" + cmd_line

                            if tname in ("read_file", "view_file") and target_path:
                                intra_session_reads[(sid, target_path)] += 1
                                has_bounds = bool(args.get("StartLine") or args.get("EndLine") or args.get("StartLineNumber"))
                                read_bounded_counts["bounded" if has_bounds else "full"] += 1
                                step_actions.append(f"{tname}({Path(target_path).name}{':bounded' if has_bounds else ':FULL'})")
                            elif cmd_line:
                                step_actions.append(normalize_cmd_signature(cmd_line)[:60])
                            else:
                                step_actions.append(tname)

                            efiles, evars = extract_env_touches(combined_arg)
                            for ef in efiles:
                                env_file_sessions[ef].add(sid)
                            for ev_name in evars:
                                env_var_sessions[ev_name].add(sid)

                            if target_path and target_path.startswith("/") and not is_rule_or_skill_path(target_path):
                                file_touch_sessions[target_path].add(sid)

                            if cmd_line:
                                sig = normalize_cmd_signature(cmd_line)
                                cmd_sessions[sig].add(sid)
                                for rg_pat in extract_rg_patterns(cmd_line):
                                    rg_pattern_sessions[rg_pat].add(sid)
                                c_low = cmd_line.lower()
                                for b_name in check_bypasses(c_low, custom_bypasses):
                                    bypass_counts[b_name].add(sid)
                        if step_actions:
                            last_action = ", ".join(step_actions[:2])
        except Exception:
            continue

    return {
        "token_totals": token_totals,
        "subagent_token_totals": subagent_token_totals,
        "subagent_parent": subagent_parent,
        "by_project_tokens": by_project_tokens,
        "by_root_tokens": by_root_tokens,
        "by_session_tokens": by_session_tokens,
        "session_titles": session_titles,
        "session_dates": session_dates,
        "session_projects": session_projects,
        "compactions_by_session": compactions_by_session,
        "tool_counts": tool_counts,
        "tool_truncations": tool_truncations,
        "truncated_fields_count": truncated_fields_count,
        "error_steps": error_steps,
        "env_file_sessions": env_file_sessions,
        "env_var_sessions": env_var_sessions,
        "file_touch_sessions": file_touch_sessions,
        "rg_pattern_sessions": rg_pattern_sessions,
        "bypass_counts": bypass_counts,
        "cmd_sessions": cmd_sessions,
        "intra_session_reads": intra_session_reads,
        "read_bounded_counts": read_bounded_counts,
        "token_spikes": token_spikes,
        "cache_busts": cache_busts,
        "cache_bust_tokens_by_session": cache_bust_tokens_by_session,
    }


def fmt_m(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def cache_pct(inp: int, cached: int) -> str:
    if inp <= 0:
        return "0.0%"
    return f"{(cached / inp) * 100:.1f}%"


def build_json_summary(days: int, start_dt: datetime, codex: dict[str, Any], agy: dict[str, Any]) -> dict[str, Any]:
    c_tok = codex["token_totals"]
    a_tok = agy["token_totals"]
    return {
        "window_days": days,
        "start_utc": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "codex": {
            "active_sessions": len(codex["by_session_tokens"]),
            "steps": c_tok["steps"],
            "input_tokens": c_tok["input_tokens"],
            "cached_input_tokens": c_tok["cached_input_tokens"],
            "uncached_input_tokens": max(0, c_tok["input_tokens"] - c_tok["cached_input_tokens"]),
            "output_tokens": c_tok["output_tokens"],
            "reasoning_output_tokens": c_tok["reasoning_output_tokens"],
            "compactions": sum(codex["compactions_by_session"].values()),
            "token_spikes": len(codex["token_spikes"]),
            "cache_busts": len(codex["cache_busts"]),
        },
        "antigravity": {
            "active_sessions": len(agy["by_session_tokens"]),
            "steps": a_tok["steps"],
            "input_tokens": a_tok["input_tokens"],
            "cached_input_tokens": a_tok["cached_input_tokens"],
            "uncached_input_tokens": max(0, a_tok["input_tokens"] - a_tok["cached_input_tokens"]),
            "output_tokens": a_tok["output_tokens"],
            "compactions": sum(agy["compactions_by_session"].values()),
            "token_spikes": len(agy["token_spikes"]),
            "cache_busts": len(agy["cache_busts"]),
            "read_bounded": agy["read_bounded_counts"]["bounded"],
            "read_full": agy["read_bounded_counts"]["full"],
        },
    }


def render_markdown(
    days: int,
    start_dt: datetime,
    codex: dict[str, Any],
    agy: dict[str, Any],
    lang: str = "en",
) -> str:
    ru = lang == "ru"
    lines: list[str] = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if ru:
        lines.append(f"# Аудит сессий агентов (за {days} дн., с {start_dt.strftime('%Y-%m-%d')} по {now_str})")
    else:
        lines.append(f"# Coding Agent Session Audit ({days}d window, {start_dt.strftime('%Y-%m-%d')} to {now_str})")
    lines.append("")

    c_tok = codex["token_totals"]
    a_tok = agy["token_totals"]
    c_uncached = max(0, c_tok["input_tokens"] - c_tok["cached_input_tokens"])
    a_uncached = max(0, a_tok["input_tokens"] - a_tok["cached_input_tokens"])

    lines.append("## 1. Сводка по токенам и эффективности кэша" if ru else "## 1. Token Usage & Prompt Cache Efficiency")
    lines.append("")
    if ru:
        lines.append("| Агент | Активных сессий | Шагов модели | Вход всего | Из кэша (%) | Вход БЕЗ кэша | Выход (output) |")
    else:
        lines.append("| Agent | Active Sessions | Model Steps | Total Input | Cached (%) | Uncached Input | Output |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    lines.append(
        f"| **Codex** | {len(codex['by_session_tokens'])} | {c_tok['steps']} | {fmt_m(c_tok['input_tokens'])} | "
        f"{fmt_m(c_tok['cached_input_tokens'])} ({cache_pct(c_tok['input_tokens'], c_tok['cached_input_tokens'])}) | "
        f"**{fmt_m(c_uncached)}** | {fmt_m(c_tok['output_tokens'])} (reas: {fmt_m(c_tok['reasoning_output_tokens'])}) |"
    )
    lines.append(
        f"| **Antigravity (`agy`)** | {len(agy['by_session_tokens'])} | {a_tok['steps']} | {fmt_m(a_tok['input_tokens'])} | "
        f"{fmt_m(a_tok['cached_input_tokens'])} ({cache_pct(a_tok['input_tokens'], a_tok['cached_input_tokens'])}) | "
        f"**{fmt_m(a_uncached)}** | {fmt_m(a_tok['output_tokens'])} |"
    )
    lines.append("")

    all_projs = set(codex["by_project_tokens"].keys()) | set(agy["by_project_tokens"].keys())
    if all_projs:
        lines.append("### Расход по проектам (Codex + Antigravity)" if ru else "### Breakdown by Project (Codex + Antigravity)")
        if ru:
            lines.append("| Проект | Шагов | Вход всего | Вход БЕЗ кэша | Выход | Доля кэша |")
        else:
            lines.append("| Project | Steps | Total Input | Uncached Input | Output | Cache Hit % |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        proj_rows = []
        for p in all_projs:
            cp = codex["by_project_tokens"][p]
            ap = agy["by_project_tokens"][p]
            inp = cp["input_tokens"] + ap["input_tokens"]
            cac = cp["cached_input_tokens"] + ap["cached_input_tokens"]
            out = cp["output_tokens"] + ap["output_tokens"]
            stp = cp["steps"] + ap["steps"]
            unc = max(0, inp - cac)
            proj_rows.append((unc + out, p, stp, inp, unc, out, cac))
        for _, p, stp, inp, unc, out, cac in sorted(proj_rows, reverse=True):
            lines.append(f"| `{p}` | {stp} | {fmt_m(inp)} | **{fmt_m(unc)}** | {fmt_m(out)} | {cache_pct(inp, cac)} |")
        lines.append("")

    lines.append(
        "## 2. Повторяющиеся раскопки между сессиями (кандидаты на автоматизацию)"
        if ru else "## 2. Cross-Session Repeated Lookups (Automation Candidates)"
    )
    lines.append("")

    all_env_files: dict[str, tuple[int, int]] = {}
    for ef in set(codex["env_file_sessions"]) | set(agy["env_file_sessions"]):
        c_n = len(codex["env_file_sessions"].get(ef, set()))
        a_n = len(agy["env_file_sessions"].get(ef, set()))
        if c_n + a_n >= 2:
            all_env_files[ef] = (c_n, a_n)

    if all_env_files:
        lines.append("### Чтение `.env` и файлов с ключами в разных сессиях" if ru else "### `.env` & Credential Files Touched Across Sessions")
        for ef, (c_n, a_n) in sorted(all_env_files.items(), key=lambda x: sum(x[1]), reverse=True)[:10]:
            lbl = "сессиях" if ru else "sessions"
            lines.append(f"- `{ef}` — **{c_n + a_n} {lbl}** (Codex: {c_n}, Antigravity: {a_n})")
        lines.append("")

    all_env_vars: dict[str, tuple[int, int]] = {}
    for ev in set(codex["env_var_sessions"]) | set(agy["env_var_sessions"]):
        c_n = len(codex["env_var_sessions"].get(ev, set()))
        a_n = len(agy["env_var_sessions"].get(ev, set()))
        if c_n + a_n >= 2:
            all_env_vars[ev] = (c_n, a_n)

    if all_env_vars:
        lines.append("### Искомые переменные окружения / секреты в командах" if ru else "### Environment Variables / Secrets Queried in Commands")
        for ev, (c_n, a_n) in sorted(all_env_vars.items(), key=lambda x: sum(x[1]), reverse=True)[:10]:
            lbl = "сессиях" if ru else "sessions"
            lines.append(f"- `{ev}` — **{c_n + a_n} {lbl}** (Codex: {c_n}, Antigravity: {a_n})")
        lines.append("")

    merged_files: dict[str, tuple[int, int]] = {}
    for fp in set(codex["file_touch_sessions"]) | set(agy["file_touch_sessions"]):
        c_n = len(codex["file_touch_sessions"].get(fp, set()))
        a_n = len(agy["file_touch_sessions"].get(fp, set()))
        if c_n + a_n >= 4:
            merged_files[fp] = (c_n, a_n)

    if merged_files:
        lines.append(
            "### Прикладные файлы, к которым обращаются чаще всего (кроме правил/навыков)"
            if ru else "### Most Frequently Read Application Files (Excluding Rules/Skills)"
        )
        for fp, (c_n, a_n) in sorted(merged_files.items(), key=lambda x: sum(x[1]), reverse=True)[:12]:
            lbl = "сессий" if ru else "sessions"
            lines.append(f"- `{fp}` — **{c_n + a_n} {lbl}** (Codex: {c_n}, Antigravity: {a_n})")
        lines.append("")

    merged_rg: dict[str, int] = {}
    for pat in set(codex["rg_pattern_sessions"]) | set(agy["rg_pattern_sessions"]):
        total_s = len(codex["rg_pattern_sessions"].get(pat, set())) + len(agy["rg_pattern_sessions"].get(pat, set()))
        if total_s >= 3:
            merged_rg[pat] = total_s
    if merged_rg:
        lines.append(
            "### Повторяющиеся поисковые запросы (`rg` / `grep`) в разных сессиях"
            if ru else "### Repeated `rg` / `grep` Searches Across Sessions"
        )
        for pat, cnt in sorted(merged_rg.items(), key=lambda x: x[1], reverse=True)[:10]:
            lbl = "сессий" if ru else "sessions"
            lines.append(f"- **{cnt} {lbl}:** `rg {pat}`")
        lines.append("")

    lines.append(
        "## 3. Сбои инструментов, циклы ожидания и работа в обход утилит"
        if ru else "## 3. Tool Failures, Polling Loops & Workflow Bypasses"
    )
    lines.append("")

    all_bypasses = sorted(set(codex["bypass_counts"]) | set(agy["bypass_counts"]))
    if all_bypasses:
        lines.append("### Работа в обход штатных утилит" if ru else "### Configured Tool Bypasses")
        for b_name in all_bypasses:
            c_n = len(codex["bypass_counts"].get(b_name, set()))
            a_n = len(agy["bypass_counts"].get(b_name, set()))
            lbl = "сессий" if ru else "sessions"
            lines.append(f"- **{b_name}:** {c_n + a_n} {lbl} (Codex: {c_n}, Antigravity: {a_n})")
        lines.append("")

    if codex["mcp_counts"]:
        lines.append("### Вызовы и ошибки MCP-инструментов (Codex)" if ru else "### MCP Tool Calls & Error Rates (Codex)")
        if ru:
            lines.append("| MCP-инструмент | Вызовов | Ошибок | % брака | Главная причина ошибки |")
        else:
            lines.append("| MCP Tool | Calls | Errors | Error % | Top Failure Reason |")
        lines.append("|---|---:|---:|---:|---|")
        for tool, cnt in codex["mcp_counts"].most_common(12):
            fails = codex["mcp_fails"][tool]
            rate = f"{(fails / cnt) * 100:.1f}%" if cnt else "0%"
            top_reason = ""
            if fails and codex["mcp_fail_reasons"].get(tool):
                r_txt, r_cnt = codex["mcp_fail_reasons"][tool].most_common(1)[0]
                top_reason = f"`{r_txt}` ({r_cnt})"
            lines.append(f"| `{tool}` | {cnt} | {fails} | {rate} | {top_reason} |")
        lines.append("")

    if codex["cmd_fails"]:
        lines.append("### Топ падающих shell-команд (Codex)" if ru else "### Top Failing Shell Commands (Codex)")
        for sig, f_cnt in codex["cmd_fails"].most_common(10):
            tot_cnt = codex["cmd_counts"][sig]
            s_cnt = len(codex["cmd_sessions"][sig])
            if ru:
                lines.append(f"- **{f_cnt} сбоев** (из {tot_cnt} вызовов в {s_cnt} сессиях): `{sig}`")
            else:
                lines.append(f"- **{f_cnt} failures** (out of {tot_cnt} calls across {s_cnt} sessions): `{sig}`")
        lines.append("")

    if codex["polling_loops"]:
        lines.append(
            "### Циклы повторов подряд (≥3 одинаковых вызовов подряд в одной сессии)"
            if ru else "### Consecutive Polling Loops (≥3 Identical Calls in a Row)"
        )
        for sig, l_cnt in codex["polling_loops"].most_common(8):
            lbl = "сессий с циклом" if ru else "sessions with loop"
            lines.append(f"- **{l_cnt} {lbl}:** `{sig}`")
        lines.append("")

    lines.append(
        "## 4. Рентген раздувания контекста (где сессии набирают лишний вес)"
        if ru else "## 4. Context Bloat X-Ray (4-Lens Breakdown)"
    )
    lines.append("")
    total_c_comp = sum(codex["compactions_by_session"].values())
    total_a_comp = sum(agy["compactions_by_session"].values())
    if ru:
        lines.append(
            f"- **Codex:** выводов команд >30 КБ — **{len(codex['big_outputs'])}**, "
            f"автосжатий контекста (`contextCompaction`) — **{total_c_comp}** (в {len(codex['compactions_by_session'])} сессиях)."
        )
    else:
        lines.append(
            f"- **Codex:** command outputs >30 KB — **{len(codex['big_outputs'])}**, "
            f"context compactions (`contextCompaction`) — **{total_c_comp}** (across {len(codex['compactions_by_session'])} sessions)."
        )
    if codex["big_output_by_sig"]:
        lines.append(
            "  Команды, чаще всего заливающие >30 КБ в контекст Codex:"
            if ru else "  Commands most frequently dumping >30 KB into Codex context:"
        )
        for sig, cnt in codex["big_output_by_sig"].most_common(6):
            lines.append(f"  - `{sig}` — **{cnt}x**")

    bounded_n = agy["read_bounded_counts"]["bounded"]
    full_n = agy["read_bounded_counts"]["full"]
    tot_reads_a = bounded_n + full_n
    full_pct = f"{(full_n / tot_reads_a) * 100:.1f}%" if tot_reads_a else "0%"
    if ru:
        lines.append(
            f"- **Antigravity (`agy`):** автосжатий (`CHECKPOINT`) — **{total_a_comp}** (в {len(agy['compactions_by_session'])} сессиях), "
            f"обрезок (`truncated_fields`) — `content`: **{agy['truncated_fields_count']['content']}**, "
            f"`tool_calls`: **{agy['truncated_fields_count']['tool_calls']}**."
        )
        lines.append(
            f"  Дисциплина чтения файлов в Antigravity (`read_file`/`view_file`): по диапазону строк (`StartLine`/`EndLine`) — "
            f"**{bounded_n}**, целиком без границ — **{full_n}** (**{full_pct}** всех чтений)."
        )
    else:
        lines.append(
            f"- **Antigravity (`agy`):** compactions (`CHECKPOINT`) — **{total_a_comp}** (across {len(agy['compactions_by_session'])} sessions), "
            f"truncations (`truncated_fields`) — `content`: **{agy['truncated_fields_count']['content']}**, "
            f"`tool_calls`: **{agy['truncated_fields_count']['tool_calls']}**."
        )
        lines.append(
            f"  File read discipline in Antigravity (`read_file`/`view_file`): line-bounded (`StartLine`/`EndLine`) — "
            f"**{bounded_n}**, full unbounded reads — **{full_n}** (**{full_pct}** of all reads)."
        )
    lines.append("")

    rereads: list[tuple[int, str, str, str]] = []
    for (sid, fp), cnt in codex["intra_session_reads"].items():
        if cnt >= 4:
            rereads.append((cnt, "Codex", sid[:12], fp))
    for (sid, fp), cnt in agy["intra_session_reads"].items():
        if cnt >= 4:
            rereads.append((cnt, "agy", sid[:12], fp))
    if rereads:
        lines.append(
            "### 4.1. Многократное перечитывание одного файла внутри ОДНОЙ сессии (≥4 раз)"
            if ru else "### 4.1. Intra-Session File Re-Reads (≥4x in a Single Session)"
        )
        for cnt, ag_name, sid_s, fp in sorted(rereads, reverse=True)[:10]:
            lines.append(f"- **{cnt}x** ({ag_name} `{sid_s}`): `{fp}`")
        lines.append("")

    all_spikes = codex["token_spikes"] + agy["token_spikes"]
    if all_spikes:
        lines.append(
            "### 4.2. Разовые скачки контекста (`token spikes` ≥ +35K токенов за шаг)"
            if ru else "### 4.2. Single-Step Context Jumps (`token spikes` ≥ +35K tokens)"
        )
        for sp in sorted(all_spikes, key=lambda x: x["jump_tokens"], reverse=True)[:8]:
            lines.append(
                f"- **+{fmt_m(sp['jump_tokens'])} tokens** (total {fmt_m(sp['total_input'])}, "
                f"{sp['agent']} `{sp['session_id'][:12]}`) after: `{sp['action']}`"
            )
        lines.append("")

    all_busts = codex["cache_busts"] + agy["cache_busts"]
    if all_busts:
        c_bust_sum = sum(x["uncached_loss"] for x in codex["cache_busts"])
        a_bust_sum = sum(x["uncached_loss"] for x in agy["cache_busts"])
        if ru:
            lines.append(
                f"### 4.3. Сбросы префиксного кэша (`cache busts` — провал кэша на длинном контексте)\n"
                f"- Всего провалов кэша (≥40K некэшированного входа за шаг): **{len(all_busts)}** "
                f"(потеряно на перезагрузке префикса — Codex: **{fmt_m(c_bust_sum)}**, Antigravity: **{fmt_m(a_bust_sum)}**)."
            )
        else:
            lines.append(
                f"### 4.3. Prefix Cache Busts (≥40K uncached input on a long context)\n"
                f"- Total cache bust events: **{len(all_busts)}** "
                f"(uncached reload cost — Codex: **{fmt_m(c_bust_sum)}**, Antigravity: **{fmt_m(a_bust_sum)}**)."
            )
        for cb in sorted(all_busts, key=lambda x: x["uncached_loss"], reverse=True)[:6]:
            lines.append(
                f"  - **+{fmt_m(cb['uncached_loss'])} uncached** (context {fmt_m(cb['new_input'])}, cached {fmt_m(cb['cached'])}, "
                f"{cb['agent']} `{cb['session_id'][:12]}`) on: `{cb['action']}`"
            )
        lines.append("")

    c_sub = codex["subagent_token_totals"]
    a_sub = agy["subagent_token_totals"]
    c_sub_unc = max(0, c_sub["input_tokens"] - c_sub["cached_input_tokens"])
    a_sub_unc = max(0, a_sub["input_tokens"] - a_sub["cached_input_tokens"])

    parent_children_codex: dict[str, list[str]] = defaultdict(list)
    for child_id, p_id in codex["subagent_parent"].items():
        if child_id in codex["by_session_tokens"]:
            parent_children_codex[p_id].append(child_id)

    parent_children_agy: dict[str, list[str]] = defaultdict(list)
    for child_id, p_id in agy["subagent_parent"].items():
        if child_id in agy["by_session_tokens"]:
            parent_children_agy[p_id].append(child_id)

    lines.append(
        "### 4.4. Вклад субагентов (ведущая сессия vs дочерние помощники)"
        if ru else "### 4.4. Subagent Sprawl (Main Session vs Spawned Subagents)"
    )
    lines.append(
        f"- **Codex subagents:** {sum(len(v) for v in parent_children_codex.values())} active child threads, "
        f"total input **{fmt_m(c_sub['input_tokens'])}**, uncached **{fmt_m(c_sub_unc)}** "
        f"({cache_pct(max(1, c_uncached), c_sub_unc)} of Codex uncached input), "
        f"output **{fmt_m(c_sub['output_tokens'])}**."
    )
    lines.append(
        f"- **Antigravity subagents:** {sum(len(v) for v in parent_children_agy.values())} active child threads, "
        f"total input **{fmt_m(a_sub['input_tokens'])}**, uncached **{fmt_m(a_sub_unc)}** "
        f"({cache_pct(max(1, a_uncached), a_sub_unc)} of Antigravity uncached input), "
        f"output **{fmt_m(a_sub['output_tokens'])}**."
    )

    sprawl_rows = []
    for p_id, children in parent_children_codex.items():
        p_u = codex["by_session_tokens"].get(p_id, Counter())
        p_unc = max(0, p_u["input_tokens"] - p_u["cached_input_tokens"])
        ch_unc = sum(max(0, codex["by_session_tokens"][c]["input_tokens"] - codex["by_session_tokens"][c]["cached_input_tokens"]) for c in children)
        ch_out = sum(codex["by_session_tokens"][c]["output_tokens"] for c in children)
        title = (codex["threads_meta"].get(p_id, {}).get("title") or codex["session_titles"].get(p_id) or "")[:50]
        sprawl_rows.append((ch_unc + ch_out, "Codex", p_id[:12], len(children), p_unc, ch_unc, ch_out, title))
    for p_id, children in parent_children_agy.items():
        p_u = agy["by_session_tokens"].get(p_id, Counter())
        p_unc = max(0, p_u["input_tokens"] - p_u["cached_input_tokens"])
        ch_unc = sum(max(0, agy["by_session_tokens"][c]["input_tokens"] - agy["by_session_tokens"][c]["cached_input_tokens"]) for c in children)
        ch_out = sum(agy["by_session_tokens"][c]["output_tokens"] for c in children)
        title = (agy["session_titles"].get(p_id) or "")[:50]
        sprawl_rows.append((ch_unc + ch_out, "agy", p_id[:12], len(children), p_unc, ch_unc, ch_out, title))

    if sprawl_rows:
        lines.append(
            "  Родительские сессии с самым большим расходом на субагентов:"
            if ru else "  Parent sessions with highest subagent token spend:"
        )
        for _, ag_name, pid_s, n_ch, p_unc, ch_unc, ch_out, title in sorted(sprawl_rows, reverse=True)[:6]:
            lines.append(
                f"  - {ag_name} `{pid_s}` — **{n_ch} subagents**, subagent uncached **{fmt_m(ch_unc)}** "
                f"(parent uncached: {fmt_m(p_unc)}), subagent output {fmt_m(ch_out)} — *{title}*"
            )
    lines.append("")

    lines.append(
        "## 5. Топ-10 самых тяжёлых сессий за период (по некэшированному входу + выходу)"
        if ru else "## 5. Top 10 Heaviest Sessions (by Uncached Input + Output)"
    )
    lines.append("")
    if ru:
        lines.append("| Агент | Дата | Проект | Сессия | Шагов | Вход всего | Вход БЕЗ кэша | Выход | Сжатий | Запрос / тема |")
    else:
        lines.append("| Agent | Date | Project | Session | Steps | Total Input | Uncached Input | Output | Compactions | Prompt / Topic |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---|")
    heavy = []
    for sid, u in codex["by_session_tokens"].items():
        meta = codex["threads_meta"].get(sid, {})
        proj = meta.get("project") or "default"
        sub_lbl = codex["subagent_label"].get(sid)
        title = meta.get("title") or codex["session_titles"].get(sid) or (f"[subagent {sub_lbl}]" if sub_lbl else "(worker/subtask)")
        dt_s = codex["session_dates"].get(sid) or ""
        unc = max(0, u["input_tokens"] - u["cached_input_tokens"])
        comp = codex["compactions_by_session"].get(sid, 0)
        heavy.append((unc + u["output_tokens"], "Codex", dt_s, proj, sid[:12], u["steps"], u["input_tokens"], unc, u["output_tokens"], comp, title))
    for sid, u in agy["by_session_tokens"].items():
        proj = agy["session_projects"].get(sid, "default")
        p_id = agy["subagent_parent"].get(sid)
        title = agy["session_titles"].get(sid) or (f"[subagent of {p_id[:8]}]" if p_id else "(agy session)")
        dt_s = agy["session_dates"].get(sid) or ""
        unc = max(0, u["input_tokens"] - u["cached_input_tokens"])
        comp = agy["compactions_by_session"].get(sid, 0)
        heavy.append((unc + u["output_tokens"], "agy", dt_s, proj, sid[:12], u["steps"], u["input_tokens"], unc, u["output_tokens"], comp, title))

    for _, ag_name, dt_s, proj, sid_short, stp, inp, unc, out, comp, title in sorted(heavy, reverse=True)[:10]:
        clean_title = title.replace("|", "/").strip()[:55]
        lines.append(
            f"| {ag_name} | {dt_s} | `{proj}` | `{sid_short}` | {stp} | {fmt_m(inp)} | **{fmt_m(unc)}** | {fmt_m(out)} | {comp} | {clean_title} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only token, cache & workflow auditor for Codex and Antigravity (agy) coding agent sessions."
    )
    ap.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    ap.add_argument("--all", action="store_true", help="Analyze all available history")
    ap.add_argument("--project", type=str, default=None, help="Filter by project slug")
    ap.add_argument("--lang", choices=("en", "ru"), default="en", help="Report language: en (default) or ru")
    ap.add_argument("--json", action="store_true", help="Output machine-readable JSON summary")
    ap.add_argument("--output", type=str, default=None, help="Write report to file (mode 0600)")
    ap.add_argument("--codex-home", action="append", default=None, help="Custom Codex home path (repeatable)")
    ap.add_argument("--agy-root", action="append", default=None, help="Custom Antigravity CLI root path (repeatable)")
    ap.add_argument("--config", type=str, default=None, help="Path to optional config.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    codex_homes = discover_codex_homes(args.codex_home, cfg)
    agy_roots = discover_agy_roots(args.agy_root, cfg)

    days = 3650 if args.all else max(1, args.days)
    start_dt = datetime.now(timezone.utc) - timedelta(days=days)

    codex_data = collect_codex(codex_homes, start_dt, args.project, cfg)
    agy_data = collect_antigravity(agy_roots, start_dt, args.project, cfg)

    if args.json:
        out_text = json.dumps(build_json_summary(days, start_dt, codex_data, agy_data), indent=2, ensure_ascii=False)
    else:
        out_text = render_markdown(days, start_dt, codex_data, agy_data, lang=args.lang)

    if args.output:
        out_p = Path(args.output)
        out_p.write_text(out_text, encoding="utf-8")
        out_p.chmod(0o600)
    print(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
