"""Tests for kb_drift.py. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "kb_drift.py"
spec = importlib.util.spec_from_file_location("kb_drift", SCRIPT)
kd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kd)


class GithubOwnerTest(unittest.TestCase):
    def test_reads_the_owner_from_ssh_and_https_remotes(self) -> None:
        for remote in ("git@github.com:acme/payments-api.git", "https://github.com/acme/payments-api",
                       "https://github.com/acme/payments-api.git/", "ssh://git@github.com/acme/payments-api.git\n"):
            self.assertEqual(kd.github_owner(remote), "acme", remote)

    def test_other_hosts_have_no_owner(self) -> None:
        for remote in ("https://gitlab.com/acme/payments-api.git", "", "/srv/git/payments-api"):
            self.assertIsNone(kd.github_owner(remote), remote)


if __name__ == "__main__":
    unittest.main()
