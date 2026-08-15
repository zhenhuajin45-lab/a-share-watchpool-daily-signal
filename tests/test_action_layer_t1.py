from action_layer import build_position_context, decide_event_actions


def _strong_sell_event(event_date: str, entry_date: str) -> dict:
    return {
        "event": "SELL_EVENT_WATCH",
        "pattern": "VIRTUAL_STOP_LOSS",
        "exit_tier": "EXIT",
        "event_ts": f"{event_date}T10:00:00",
        "position_entry_date": entry_date,
    }


def test_same_day_signal_entry_is_t1_locked() -> None:
    decision = decide_event_actions(
        _strong_sell_event("2026-08-15", "2026-08-15"),
        {},
    )

    assert decision["t_plus_one_locked"] is True
    assert decision["existing_position"]["code"] == "T1_LOCKED_STOP_ADDING"
    assert decision["position_context"]["broker_position_known"] is False


def test_prior_day_signal_entry_can_exit() -> None:
    decision = decide_event_actions(
        _strong_sell_event("2026-08-15", "2026-08-14"),
        {},
    )

    assert decision["t_plus_one_locked"] is False
    assert decision["existing_position"]["code"] == "EXIT"


def test_broker_quantities_are_not_inferred_from_signal_ledger() -> None:
    unknown = build_position_context("2026-08-15", "2026-08-14")
    known = build_position_context(
        "2026-08-15",
        "2026-08-14",
        {"total_qty": 1000, "sellable_qty": 600, "today_bought_qty": 400},
    )

    assert unknown["quantity_boundary"] == "UNKNOWN_NOT_INFERRED_FROM_SIGNAL_LEDGER"
    assert unknown["sellable_qty"] is None
    assert known["quantity_boundary"] == "BROKER_FACT"
    assert known["total_qty"] == 1000
    assert known["sellable_qty"] == 600
    assert known["today_bought_qty"] == 400
