from __future__ import annotations

MIN_LIKES_FOR_REVIEW = 10


def build_feedback_report(rows):
    groups = []
    for category, model, prompt_version, total, positive, negative in rows:
        qualified = positive >= MIN_LIKES_FOR_REVIEW
        groups.append(
            {
                "category": category,
                "model": model,
                "prompt_version": prompt_version,
                "ratings": total,
                "up": positive,
                "down": negative,
                "approval_percent": round(positive / total * 100, 1) if total else 0,
                "ready_to_compare": total >= 10,
                "qualified_for_review": qualified,
                "likes_needed": max(0, MIN_LIKES_FOR_REVIEW - positive),
                "recommendations": [],
                "approval_required": qualified,
            }
        )

    for group in groups:
        if not group["qualified_for_review"]:
            continue
        configuration = (
            f"category={group['category']}, model={group['model']}, "
            f"prompt_version={group['prompt_version']}"
        )
        alternatives = sorted(
            {
                f"model={other['model']}, prompt_version={other['prompt_version']}"
                for other in groups
                if other["category"] == group["category"]
                and (other["model"], other["prompt_version"])
                != (group["model"], group["prompt_version"])
                and other["ready_to_compare"]
            }
        )
        comparison = (
            f"against these recorded configurations: {'; '.join(alternatives)}"
            if alternatives
            else "against another explicitly identified model/prompt version in the "
            "same category once comparable feedback is available"
        )
        group["recommendations"] = [
            f"Inspect positive and negative feedback for {configuration}; ask "
            "requesters for examples to review because reply text is not stored.",
            f"Compare {configuration} {comparison}, checking rating counts and "
            "approval percentages under comparable conditions.",
            f"Propose one specific prompt edit or model switch for {configuration}, "
            "with the exact prompt change or target model, supporting examples, "
            "and an evaluation plan; submit it for administrator approval before applying it.",
            "Aggregate metadata contains rating counts and configuration identifiers, "
            "not prompts, reply text, or conversation context. It cannot explain "
            "why users voted, establish causality, or determine a specific improvement. "
            "Aggregate metadata cannot diagnose individual answer failures or establish "
            "that another configuration is better.",
        ]

    return {
        "required_likes": MIN_LIKES_FOR_REVIEW,
        "qualified_group_count": sum(group["qualified_for_review"] for group in groups),
        "approval_required": True,
        "groups": groups,
    }
