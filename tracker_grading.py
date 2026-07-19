def grade_matches_label(grade: str, label: str) -> bool:
    """Check if a graded verdict matches the expected label (win/loss)."""
    return (grade == "WIN" and label == "win") or (grade == "LOSS" and label == "loss")


def _overall_verdict(verdicts: list[tuple]) -> str:
    """
    Collapse per-pick verdicts into a single message verdict.

    Parlay legs: ALL must WIN → WIN; any LOSS → LOSS; any UNKNOWN → UNKNOWN.
    Non-parlay:  all must agree (all WIN or all LOSS); mixed or any UNKNOWN → UNKNOWN.
    """
    if not verdicts:
        return "UNKNOWN"
    all_v = [v[1] for v in verdicts]
    is_parlay = any(v[0].get("is_parlay_leg") for v in verdicts)
    if is_parlay:
        # A parlay is lost the instant ANY leg loses — the remaining legs
        # (even still-PENDING ones) can't change the outcome, so LOSS settles
        # it immediately and must be checked before PENDING/UNKNOWN.
        if "LOSS" in all_v:
            return "LOSS"
        if "PENDING" in all_v:
            return "PENDING"
        if "UNKNOWN" in all_v:
            return "UNKNOWN"
        if all(v == "WIN" for v in all_v):
            return "WIN"
        if "PUSH" in all_v:
            return "PUSH"
        return "UNKNOWN"
    else:
        unique = set(all_v) - {"PUSH"}
        if "PENDING" in unique:
            return "PENDING"
        if "UNKNOWN" in unique or len(unique) > 1:
            return "UNKNOWN"
        return unique.pop() if unique else "PUSH"
