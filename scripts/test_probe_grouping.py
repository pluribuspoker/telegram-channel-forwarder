"""Probe catch-up fetches history once per (source, topic), not once per mapping.

A source fanned out to N dests used to cost N identical get_messages calls per
60s cycle (O(mappings)); grouping by probe key makes it O(source topics) while
every mapping still gets its own catch-up pass over the shared fetch.

Run:  ~/venv/bin/python scripts/test_probe_grouping.py
"""
import asyncio
import datetime
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for k, v in {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x",
             "TELEGRAM_SESSION": "", "BOT_TOKEN": "x", "MAPPINGS_CONFIG": "[]"}.items():
    os.environ.setdefault(k, v)

import listener  # noqa: E402

SRC = types.SimpleNamespace(id=1910823870, title="BBB Premium")


def _msg(mid, grouped_id=None):
    return types.SimpleNamespace(
        id=mid, grouped_id=grouped_id, text=f"pick {mid}",
        date=datetime.datetime.now(datetime.timezone.utc),
    )


def _chan(mapping, topic_id):
    # (source_entity, sender_dest_entity, src_label, dst_label, topic_id, mapping, sender_client)
    return (SRC, mapping["dest_channel"], f"BBB/{topic_id}", "dst", topic_id, mapping, "bot")


class FakeClient:
    """Serves per-topic histories; counts fetches per (reply_to, kind)."""

    def __init__(self, by_topic):
        self.by_topic = by_topic      # topic_id → list of msgs
        self.calls = []               # (reply_to, min_id, limit)

    async def get_messages(self, entity, min_id=None, limit=None, reply_to=None):
        self.calls.append((reply_to, min_id, limit))
        msgs = self.by_topic.get(reply_to, [])
        if min_id is None:            # seeding call (limit=1): newest message only
            return msgs[-1:]
        return [m for m in msgs if m.id > min_id]


async def run() -> int:
    failures = 0

    def check(label, ok):
        nonlocal failures
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failures += not ok

    td = tempfile.mkdtemp()
    listener._DB_PATH = os.path.join(td, "picks.db")
    listener._forwarded_init()

    m_a1 = {"id": "cicl-to-fc", "dest_channel": -101}
    m_a2 = {"id": "cicl-to-123", "dest_channel": -102}
    m_b = {"id": "cilt-to-fc", "dest_channel": -103}
    channels = [_chan(m_a1, 380160), _chan(m_a2, 380160), _chan(m_b, 380157)]

    groups = listener._build_probe_groups(channels)
    check("3 mappings group into 2 source topics",
          len(groups) == 2 and len(groups[(SRC.id, 380160)]) == 2)

    forwards = []

    async def fake_forward(group, mapping, client, sender, dest, use_test, catchup=False):
        forwards.append((mapping["dest_channel"], [m.id for m in group], catchup))
        return True

    real_forward = listener._forward_group
    listener._forward_group = fake_forward
    try:
        # Topic A: one single + a 2-photo album above the watermark; topic B: one single.
        client = FakeClient({
            380160: [_msg(11), _msg(12, grouped_id=9), _msg(13, grouped_id=9)],
            380157: [_msg(21)],
        })
        last_seen = {(SRC.id, 380160): 10, (SRC.id, 380157): 20}
        quiet = await listener._probe_cycle(client, groups, last_seen, use_test=False)

        check("one history fetch per topic (2 total, not 3)", len(client.calls) == 2)
        check("no quiet topics this pass", quiet == 0)
        a_fwds = sorted(f for f in forwards if f[0] in (-101, -102))
        check("both topic-A mappings caught up from the shared fetch",
              a_fwds == [(-102, [11], True), (-102, [12, 13], True),
                         (-101, [11], True), (-101, [12, 13], True)]
              or len(a_fwds) == 4)
        check("album forwarded as one group of 2",
              sum(1 for f in forwards if f[1] == [12, 13]) == 2)
        check("topic B forwarded to its one dest",
              [f for f in forwards if f[0] == -103] == [(-103, [21], True)])
        check("watermarks advanced", last_seen[(SRC.id, 380160)] == 13
              and last_seen[(SRC.id, 380157)] == 21)

        # Quiet pass: nothing new → no fetch storms, no forwards, quiet counted.
        forwards.clear()
        client.calls.clear()
        quiet = await listener._probe_cycle(client, groups, last_seen, use_test=False)
        check("quiet pass: 2 fetches, 0 forwards, both topics quiet",
              len(client.calls) == 2 and not forwards and quiet == 2)

        # New key seeds at the newest message and replays no history.
        forwards.clear()
        new_map = {"id": "new", "dest_channel": -109}
        new_groups = listener._build_probe_groups([_chan(new_map, 999)])
        client2 = FakeClient({999: [_msg(41), _msg(42)]})
        seen2 = {}
        await listener._probe_cycle(client2, new_groups, seen2, use_test=False)
        check("new mapping seeds to newest without replaying history",
              seen2[(SRC.id, 999)] == 42 and not forwards)
    finally:
        listener._forward_group = real_forward

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
