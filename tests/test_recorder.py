"""Tests for analytics.recorder.OpportunityRecorder's trade-result linkage.

`attach_trade_result` is what lets the dashboard show a $ P&L next to the
specific opportunity that produced it, rather than only an aggregate --
see main.py's detection loop, which calls it right after a paper/live
trade executes.
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
