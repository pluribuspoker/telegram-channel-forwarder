"""Regression tests for NFL fetcher environment precedence."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.fetch_nfl_lines import _load_environment


class EnvironmentPrecedenceTest(unittest.TestCase):
    def test_process_environment_wins_over_dotenv_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("ODDS_API_KEY=shared\n")
            (root / ".env.local").write_text("ODDS_API_KEY=local\n")

            with patch.dict(
                os.environ,
                {"ODDS_API_KEY": "service-scoped"},
                clear=False,
            ):
                _load_environment(root)

                self.assertEqual(
                    os.environ["ODDS_API_KEY"], "service-scoped"
                )

    def test_local_dotenv_wins_over_shared_for_direct_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("ODDS_API_KEY=shared\n")
            (root / ".env.local").write_text("ODDS_API_KEY=local\n")

            environment = dict(os.environ)
            environment.pop("ODDS_API_KEY", None)
            with patch.dict(os.environ, environment, clear=True):
                _load_environment(root)

                self.assertEqual(os.environ["ODDS_API_KEY"], "local")


if __name__ == "__main__":
    unittest.main()
