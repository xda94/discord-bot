"""Pure price-alert classification for tracked wishlist items."""

from __future__ import annotations

import statistics
from dataclasses import dataclass


ALERT_MIN_DATA_POINTS = 7
ALERT_LOW_REALERT_DROP_PCT = 0.01


@dataclass
class AlertDecision:
    alert_kind: str | None
    new_state: str | None
    new_state_price: float | None
    all_time_low: float | None = None
    median_price: float | None = None
    prev_alert_price: float | None = None


def classify_price(
    current: float | None,
    history: list[float | None],
    last_alert_kind: str | None,
    last_alert_price: float | None,
) -> AlertDecision:
    """Return the alert and persistent zone state for one price observation."""
    if current is None:
        return AlertDecision(None, last_alert_kind, last_alert_price)

    numeric_history = [p for p in history if isinstance(p, (int, float))]
    if len(numeric_history) < ALERT_MIN_DATA_POINTS:
        return AlertDecision(None, last_alert_kind, last_alert_price)

    all_time_low = min(numeric_history)
    median_price = statistics.median(numeric_history)
    all_observations = numeric_history + [current]
    observed_max = max(all_observations)
    observed_min = min(all_observations)
    if observed_max < observed_min * (1 + ALERT_LOW_REALERT_DROP_PCT):
        return AlertDecision(
            None,
            last_alert_kind,
            last_alert_price,
            all_time_low=all_time_low,
            median_price=median_price,
            prev_alert_price=last_alert_price,
        )

    if current <= all_time_low:
        zone = "low"
    elif current > median_price:
        zone = "high"
    else:
        zone = None

    alert = None
    new_state_price = last_alert_price
    if zone == "low":
        if last_alert_kind != "low" or (
            last_alert_price is not None
            and current <= last_alert_price * (1 - ALERT_LOW_REALERT_DROP_PCT)
        ):
            alert = "low"
            new_state_price = current
    elif zone == "high":
        if last_alert_kind != "high":
            alert = "high"
            new_state_price = current
    else:
        new_state_price = None

    return AlertDecision(
        alert_kind=alert,
        new_state=zone,
        new_state_price=new_state_price,
        all_time_low=all_time_low,
        median_price=median_price,
        prev_alert_price=last_alert_price,
    )


# Preserve the established internal name while callers migrate.
_classify_price = classify_price

