"""Tests for analytics.recorder.OpportunityRecorder's trade-result linkage
and bounded in-memory history.

`attach_trade_result` is what lets the dashboard show a $ P&L next to the
specific opportunity that produced it, rather than only an aggregate --
see main.py's detection loop, which calls it right after a paper/live
trade executes.

The bounded-history tests exist because a real multi-hour deployment was
OOM-killed by the OS: `all_opportunity_records`/`all_decay_records` used
to be plain lists that grew for the entire life of the process. Capping
them changed `attach_trade_result` from a position-based lookup (valid
only because nothing was ever evicted) to an id-based one via `_by_id`,
which is what these tests guard.
"""

from __future__ import annotations

from analytics.recorder import OpportunityRecorder
from strategies.base import Leg, Opportunity


def _opportunity() -> Opportunity:
    return Opportunity(
        strategy="cross_exchange",
        symbol="BTC/USDT",
        legs=(Leg("binance", "BTC/USDT", "buy", 100.0, 1.0, 0.001), Leg("kraken", "BTC/USDT", "sell", 102.0, 1.0, 0.001)),
        gross_profit_pct=2.0,
        net_profit_pct=1.8,
        max_size_usd=100.0,
    )


def test_record_defaults_pnl_to_none(tmp_path):
    recorder = OpportunityRecorder(output_dir=str(tmp_path))

    opportunity_id = recorder.record(_opportunity())

    record = recorder.all_opportunity_records[opportunity_id]
    assert record["pnl_usd"] is None
    assert record["trade_mode"] is None


def test_attach_trade_result_updates_the_matching_record(tmp_path):
    recorder = OpportunityRecorder(output_dir=str(tmp_path))
    id_a = recorder.record(_opportunity())
    id_b = recorder.record(_opportunity())

    recorder.attach_trade_result(id_b, pnl_usd=4.20, mode="paper")

    assert recorder.all_opportunity_records[id_a]["pnl_usd"] is None
    assert recorder.all_opportunity_records[id_b]["pnl_usd"] == 4.20
    assert recorder.all_opportunity_records[id_b]["trade_mode"] == "paper"


def test_attach_trade_result_ignores_out_of_range_id(tmp_path):
    recorder = OpportunityRecorder(output_dir=str(tmp_path))
    recorder.record(_opportunity())

    recorder.attach_trade_result(999, pnl_usd=1.0, mode="paper")  # must not raise

    assert recorder.all_opportunity_records[0]["pnl_usd"] is None


def test_opportunity_history_is_bounded_in_memory():
    recorder = OpportunityRecorder(output_dir="/tmp", max_in_memory_opportunities=5)

    for _ in range(20):
        recorder.record(_opportunity())

    assert len(recorder.all_opportunity_records) == 5
    # Ids keep incrementing even though old records get evicted.
    assert recorder.all_opportunity_records[-1]["id"] == 19


def test_attach_trade_result_still_works_by_id_after_older_records_evicted():
    recorder = OpportunityRecorder(output_dir="/tmp", max_in_memory_opportunities=3)

    for _ in range(10):
        last_id = recorder.record(_opportunity())

    recorder.attach_trade_result(last_id, pnl_usd=9.99, mode="paper")

    assert recorder.all_opportunity_records[-1]["pnl_usd"] == 9.99


def test_attach_trade_result_is_a_noop_once_the_record_has_aged_out():
    recorder = OpportunityRecorder(output_dir="/tmp", max_in_memory_opportunities=3)

    evicted_id = recorder.record(_opportunity())
    for _ in range(5):
        recorder.record(_opportunity())

    recorder.attach_trade_result(evicted_id, pnl_usd=1.0, mode="paper")  # must not raise

    assert evicted_id not in recorder._by_id


async def test_decay_records_are_bounded_in_memory():
    recorder = OpportunityRecorder(output_dir="/tmp", max_in_memory_decay_records=4)
    opportunity = _opportunity()

    for _ in range(10):
        await recorder._check_after_delay(0, opportunity, delay=0.0, rescan_fn=lambda: 1.0)

    assert len(recorder.all_decay_records) == 4
