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
            "one of the arguments --show-input --agent-response --api is "
            "required",
            result.stderr,
        )

    def test_generation_modes_are_mutually_exclusive(self) -> None:
        result = self._run(
            "--show-input",
            "--api",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument --show-input", result.stderr)


if __name__ == "__main__":
    unittest.main()
