"""Regression test: the VPS .env is the source of truth.

Offline, temp dirs only (no DM is sent — `send` is stubbed):

    ~/venv/bin/python scripts/test_env_guard.py

`syncenv` pushed the desktop .env over the server's: a stale copy deleted
server keys (X cookies, 2026-07-19) and resurrected removed mappings (Tony POD
2026-09-19; dfav, removed 2026-09-28). deploy/env_backup.py now reconciles any
foreign write back to the newest snapshot; scripts/set_env_local.py writes are
sanctioned by snapshotting first.

  1. reconcile keeps the baseline byte-for-byte, appends only NEW keys,
     rejects changed / dropped / tombstoned ones
  2. guard: a push is reverted + DM'd; an additions-only push is silent
  3. set_env_local.py writes stick (guard no-op); --unset tombstones the key,
     and a later push re-adding it is rejected
  4. env_mappings encode round-trips a regex through dotenv (4-backslash rule)
  5. the DM shows each rejected change (old → new), secrets as their last 4
     chars only, MAPPINGS_CONFIG as mapping ids added/removed/changed fields
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "scripts"))

import env_backup  # noqa: E402
import env_mappings  # noqa: E402

BASE = "# comment\nA=1\nMAPPINGS_CONFIG='[{\"id\":\"keep\"}]'\nB=two\n"


def _setup():
    d = Path(tempfile.mkdtemp())
    (d / "backups").mkdir()
    env_backup.BACKUP_DIR = d / "backups"
    sent = []
    env_backup.send = lambda text: sent.append(text) or True
    target = d / ".env"
    target.write_text(BASE)
    env_backup.record_snapshot(".env", BASE.encode())
    return d, target, sent


def test_reconcile():
    pushed = "A=1\nMAPPINGS_CONFIG='[{\"id\":\"keep\"},{\"id\":\"dfav\"}]'\nNEW=x\nGONE=y\n"
    merged, added, changed, dropped, res = env_backup.reconcile(pushed, BASE, {"GONE"})
    assert merged == BASE + "NEW=x\n", merged
    assert added == ["NEW"] and changed == ["MAPPINGS_CONFIG"] and dropped == ["B"], (added, changed, dropped)
    assert res == ["GONE"]
    print("PASS 1 reconcile: baseline kept, NEW appended, rest rejected")


def test_guard():
    d, target, sent = _setup()
    target.write_text("A=changed\nNEW=x\n")
    env_backup.guard(target, target.read_bytes())
    assert target.read_text() == BASE + "NEW=x\n", target.read_text()
    assert len(sent) == 1, sent
    dm = sent[0]
    assert "~ A: 1 → changed (rejected)" in dm, dm
    assert "− B: missing from push (kept)" in dm and "− MAPPINGS_CONFIG: missing" in dm, dm
    assert "+ NEW=x (accepted)" in dm, dm
    # the reconciled file is the new baseline → a second pass is a no-op
    env_backup.guard(target, target.read_bytes())
    assert len(sent) == 1
    # additions-only push: accepted silently
    target.write_text(target.read_text() + "MORE=1\n")
    env_backup.guard(target, target.read_bytes())
    assert target.read_text().endswith("NEW=x\nMORE=1\n") and len(sent) == 1
    print("PASS 2 guard: push reverted + one DM; re-run no-op; additions-only silent")


def test_set_env_local_sanctioned():
    d, target, sent = _setup()
    env = dict(os.environ, ENV_BACKUP_DIR=str(env_backup.BACKUP_DIR))
    tool = [sys.executable, str(ROOT / "scripts" / "set_env_local.py"), "--file", str(target)]
    subprocess.run(tool + ["A=42", "--unset", "B"], check=True, env=env, capture_output=True)
    assert "A=42\n" in target.read_text() and "B=" not in target.read_text()
    env_backup.guard(target, target.read_bytes())
    assert "A=42\n" in target.read_text() and not sent, sent
    assert env_backup.load_tombstones(".env") == {"B"}
    target.write_text(target.read_text() + "B=two\n")   # stale desktop push
    env_backup.guard(target, target.read_bytes())
    assert "B=" not in target.read_text() and len(sent) == 1 and "removed on purpose" in sent[0]
    subprocess.run(tool + ["B=back"], check=True, env=env, capture_output=True)
    assert env_backup.load_tombstones(".env") == set()
    print("PASS 3 set_env_local sticks; --unset tombstones; re-set clears it")


def test_dm_detail():
    assert env_backup.show("BOT_TOKEN", "123456:abcdefa1f9") == "…a1f9"
    assert env_backup.show("TELEGRAM_SESSION", "'short'") == "…"
    assert env_backup.show("GOD_JUDGE_SAMPLES", "3") == "3"
    old = env_mappings.encode([{"id": "keep", "f": r"^\d+"}, {"id": "dfav"}])
    new = env_mappings.encode([{"id": "keep", "f": r"^\d+U"}, {"id": "dagger"}])
    assert env_backup.describe_change("MAPPINGS_CONFIG", old, new) == "+ dagger, − dfav, keep (f)"
    assert env_backup.describe_change("X_AUTH_TOKEN", "'aaaaaaaa1111'", "'bbbbbbbb2222'") == "…1111 → …2222"
    print("PASS 5 DM detail: secrets masked to last 4, mappings as ids/fields")


def test_mappings_roundtrip():
    m = [{"id": "x", "filter_pattern": r"(?i)^\d+\s*U\b", "no_broadcast": True}]
    raw = env_mappings.encode(m)
    # one regex backslash → two in JSON → four on disk (the documented rule)
    assert "\\\\\\\\d" in raw and "\\" * 5 not in raw, raw
    assert env_mappings.decode(raw) == m
    live = Path(ROOT / ".env")
    if live.exists():
        from dotenv import dotenv_values
        cur = json.loads(dotenv_values(live)["MAPPINGS_CONFIG"])
        assert env_mappings.decode(env_mappings.encode(cur)) == cur
    print("PASS 4 env_mappings round-trip (incl. the live MAPPINGS_CONFIG)")


if __name__ == "__main__":
    test_reconcile()
    test_guard()
    test_set_env_local_sanctioned()
    test_mappings_roundtrip()
    test_dm_detail()
