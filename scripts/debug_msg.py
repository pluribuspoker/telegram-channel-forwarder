"""Debug script to inspect message text and trace emoji insertion."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(".env")
load_dotenv(".env.local", override=True)

from tracker_format import _insert_emojis, _insert_odds


def test_emoji_insertion():
    """Reproduce the bug: simulate the second partial edit for message 2287."""
    # prev_caption from the grades table (text at time of second edit)
    # Defenders already has ✅ from first edit, Stallions has no emoji yet
    prev_text_html = (
        "Andrew Cunningham\n\n"
        "• Defenders -3.5 (-115) / (3.45u to win 3u) [-165]✅\n\n"
        "• Stallions ML (-130) / (5.20u to win 4u) [-132]\n\n"
        "<blockquote>UFL &gt;=2U: \n"
        "28-13 overall\n"
        "8-5 Fav Spread\n"
        "7-2 Fav ML</blockquote>"
    )

    picks = [
        {
            "description": "DC Defenders -3.5 (-115)",
            "teams": ["DC Defenders"],
            "is_parlay_leg": False,
            "line": -3.5,
        },
        {
            "description": "Birmingham Stallions ML (-130)",
            "teams": ["Birmingham Stallions"],
            "is_parlay_leg": False,
            "line": None,
        },
    ]

    all_verdicts = [
        (picks[0], "WIN", "calc0", "UFL"),
        (picks[1], "LOSS", "calc1", "UFL"),
    ]

    # BUG: passing ALL verdicts when Defenders is already broadcast
    print("=== BUG: passing ALL verdicts (including already-broadcast Defenders) ===")
    result_bug = _insert_emojis(prev_text_html, all_verdicts)
    print(result_bug)
    print()

    # FIX: only pass newly-resolved verdicts (exclude already-broadcast pick 0)
    print("=== FIX: passing only newly-resolved verdicts ===")
    new_verdicts = [all_verdicts[1]]  # only Stallions
    result_fix = _insert_emojis(prev_text_html, new_verdicts)
    print(result_fix)
    print()

    # Verify correctness
    assert "[-132]❌" in result_fix, "Stallions should have ❌"
    assert "[-165]✅" in result_fix, "Defenders should keep ✅"
    assert "Fav ML</blockquote>" in result_fix, "Stats should have no emoji"
    print("✓ All assertions passed!")


if __name__ == "__main__":
    test_emoji_insertion()
