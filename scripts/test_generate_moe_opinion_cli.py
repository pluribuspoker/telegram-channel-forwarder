#!/usr/bin/env python3
"""Tests for explicit MOE generation backend selection."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "generate_moe_opinion.py"


class GenerateMoeOpinionCliTests(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--event-id", "unused", *arguments],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

    def test_requires_explicit_generation_mode(self) -> None:
        result = self._run()

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "one of the arguments --show-input --agent-response --api "
            "--deterministic is required",
            result.stderr,
        )

    def test_generation_modes_are_mutually_exclusive(self) -> None:
        result = self._run(
            "--show-input",
            "--api",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument --show-input", result.stderr)

    def test_generation_effort_requires_agent_response(self) -> None:
        result = self._run(
            "--api",
            "--generation-effort",
            "max",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "--generation-effort requires --agent-response",
            result.stderr,
        )

    def test_input_file_is_refused_with_api(self) -> None:
        result = self._run("--api", "--input-file", "input.json")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--input-file is not valid with --api", result.stderr)

    def test_generation_backend_requires_agent_response(self) -> None:
        result = self._run(
            "--deterministic",
            "--generation-backend",
            "claude_headless",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "--generation-backend requires --agent-response",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
