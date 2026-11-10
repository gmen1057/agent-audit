from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import agent_audit


class AgentAuditTests(unittest.TestCase):
    def test_extract_env_touches_redacts_dummies_and_keeps_names_only(self) -> None:
        cmd = "grep DATABASE_URL /app/backend/.env && OPENAI_API_KEY=dummy pytest"
        files, env_vars = agent_audit.extract_env_touches(cmd)
        self.assertIn("/app/backend/.env", files)
        self.assertIn("DATABASE_URL", env_vars)
        self.assertNotIn("OPENAI_API_KEY", env_vars)

    def test_codex_and_agy_collection_and_deduplication(self) -> None:
        now = datetime.now(timezone.utc)
        ts_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            (codex_home / "sessions" / "2026").mkdir(parents=True)

            # Create state_5.sqlite
            state_db = codex_home / "state_5.sqlite"
            conn = sqlite3.connect(state_db)
            conn.execute(
                """
                CREATE TABLE threads (
                    id TEXT, cwd TEXT, model TEXT, reasoning_effort TEXT,
                    tokens_used INTEGER, created_at INTEGER, updated_at INTEGER,
                    title TEXT, first_user_message TEXT, source TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "thread-main-1",
                    "/workspace/acme-app",
                    "gpt-5",
                    "high",
                    1000,
                    int(now.timestamp()),
                    int(now.timestamp()),
                    "Fix billing webhook",
                    "Fix billing webhook",
                    "",
                ),
            )
            conn.commit()
            conn.close()

            # Create rollout jsonl with duplicate response_id + token spike + cache bust
            rollout = codex_home / "sessions" / "2026" / "rollout-1.jsonl"
            records = [
                {"type": "session_meta", "timestamp": ts_iso, "payload": {"id": "thread-main-1", "cwd": "/workspace/acme-app"}},
                {
                    "type": "response_item",
                    "timestamp": ts_iso,
                    "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "git diff"})},
                },
                {
                    "type": "token_usage_record",
                    "timestamp": ts_iso,
                    "payload": {
                        "response_id": "resp-1",
                        "thread_id": "thread-main-1",
                        "usage": {"input_tokens": 20000, "cached_input_tokens": 15000, "output_tokens": 500, "reasoning_output_tokens": 200},
                    },
                },
                # Duplicate response_id should be ignored
                {
                    "type": "token_usage_record",
                    "timestamp": ts_iso,
                    "payload": {
                        "response_id": "resp-1",
                        "thread_id": "thread-main-1",
                        "usage": {"input_tokens": 20000, "cached_input_tokens": 15000, "output_tokens": 500, "reasoning_output_tokens": 200},
                    },
                },
                # Jump from 20K -> 65K (+45K spike) + edit tool call starting implementation phase
                {
                    "type": "response_item",
                    "timestamp": ts_iso,
                    "payload": {"type": "function_call", "name": "apply_patch", "arguments": "*** Begin Patch"},
                },
                {
                    "type": "token_usage_record",
                    "timestamp": ts_iso,
                    "payload": {
                        "response_id": "resp-2",
                        "thread_id": "thread-main-1",
                        "usage": {"input_tokens": 65000, "cached_input_tokens": 60000, "output_tokens": 600, "reasoning_output_tokens": 100},
                    },
                },
                # Cache bust: prev_inp=65K, new cached=10K (<50%), uncached=60K (>=40K)
                {
                    "type": "token_usage_record",
                    "timestamp": ts_iso,
                    "payload": {
                        "response_id": "resp-3",
                        "thread_id": "thread-main-1",
                        "usage": {"input_tokens": 70000, "cached_input_tokens": 10000, "output_tokens": 400, "reasoning_output_tokens": 100},
                    },
                },
            ]
            rollout.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

            # Antigravity transcript
            agy_root = root / ".gemini" / "antigravity-cli"
            agy_logs = agy_root / "brain" / "11111111-2222-3333-4444-555555555555" / ".system_generated" / "logs"
            agy_logs.mkdir(parents=True)
            transcript = agy_logs / "transcript.jsonl"
            agy_events = [
                {"type": "USER_INPUT", "created_at": ts_iso, "content": "<USER_REQUEST>Audit acme-app</USER_REQUEST>"},
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 1,
                    "created_at": ts_iso,
                    "input_tokens": 4000,
                    "cache_read_tokens": 16000,
                    "output_tokens": 300,
                    "tool_calls": [
                        {"name": "mcp_codegraph_find_symbol", "arguments": {"project": "acme-app", "name": "handle_webhook"}},
                        {"name": "read_file", "arguments": {"Path": "/workspace/acme-app/main.py", "StartLine": 1, "EndLine": 50}},
                        {"name": "read_file", "arguments": {"Path": "/workspace/acme-app/monolith.py"}},
                    ],
                },
                {
                    "type": "PLANNER_RESPONSE",
                    "step_index": 2,
                    "created_at": ts_iso,
                    "input_tokens": 6000,
                    "cache_read_tokens": 18000,
                    "output_tokens": 250,
                    "tool_calls": [
                        {"name": "edit_file", "arguments": {"Path": "/workspace/acme-app/main.py", "Target": "a", "Replacement": "b"}},
                    ],
                },
            ]
            transcript.write_text("\n".join(json.dumps(e) for e in agy_events) + "\n", encoding="utf-8")

            start_dt = now - timedelta(days=1)
            codex_data = agent_audit.collect_codex([codex_home], start_dt, None, {})
            agy_data = agent_audit.collect_antigravity([("default", agy_root)], start_dt, None, {})

            # Verify Codex deduplication & totals (20K + 65K + 70K = 155K)
            self.assertEqual(codex_data["token_totals"]["steps"], 3)
            self.assertEqual(codex_data["token_totals"]["input_tokens"], 155000)
            self.assertEqual(codex_data["token_totals"]["cached_input_tokens"], 85000)
            self.assertEqual(len(codex_data["token_spikes"]), 1)
            self.assertEqual(len(codex_data["cache_busts"]), 1)
            self.assertEqual(codex_data["edit_sessions_count"], 1)
            # Step 1 (5K uncached) is before 1st edit ('a'); Steps 2+3 (5K + 60K = 65K uncached) are 'b'
            self.assertEqual(codex_data["phase_tokens"]["a"], 5000)
            self.assertEqual(codex_data["phase_tokens"]["b"], 65000)
            self.assertEqual(codex_data["phase_steps"]["a"], 1)
            self.assertEqual(codex_data["phase_steps"]["b"], 2)

            # Verify Antigravity accounting (step 1: 4K+16K=20K; step 2: 6K+18K=24K -> total 44K, cached 34K)
            self.assertEqual(agy_data["token_totals"]["steps"], 2)
            self.assertEqual(agy_data["token_totals"]["input_tokens"], 44000)
            self.assertEqual(agy_data["token_totals"]["cached_input_tokens"], 34000)
            self.assertEqual(agy_data["read_bounded_counts"]["bounded"], 1)
            self.assertEqual(agy_data["read_bounded_counts"]["full"], 1)
            self.assertEqual(agy_data["edit_sessions_count"], 1)
            self.assertEqual(agy_data["phase_tokens"]["a"], 4000)
            self.assertEqual(agy_data["phase_tokens"]["b"], 6000)
            self.assertEqual(agy_data["codegraph_counts"]["mcp_codegraph_find_symbol"], 1)

            md_en = agent_audit.render_markdown(1, start_dt, codex_data, agy_data, lang="en")
            md_ru = agent_audit.render_markdown(1, start_dt, codex_data, agy_data, lang="ru")
            self.assertIn("Coding Agent Session Audit", md_en)
            self.assertIn("Phase Breakdown: Exploration vs Implementation KPI", md_en)
            self.assertIn("Codegraph Adoption in Antigravity", md_en)
            self.assertIn("overlapping slices of total token spend", md_en)
            self.assertIn("Аудит сессий агентов", md_ru)
            self.assertIn("Фазовый разрез: разведка vs реализация (Exploration vs Implementation KPI)", md_ru)
            self.assertIn("Использование codegraph в Antigravity", md_ru)
            self.assertIn("пересекающиеся срезы общего расхода", md_ru)

            summary = agent_audit.build_json_summary(1, start_dt, codex_data, agy_data)
            self.assertEqual(summary["codex"]["edit_sessions"], 1)
            self.assertEqual(summary["codex"]["phase_uncached_tokens"]["exploration_before_first_edit"], 5000)
            self.assertEqual(summary["antigravity"]["codegraph_calls"], 1)


if __name__ == "__main__":
    unittest.main()
