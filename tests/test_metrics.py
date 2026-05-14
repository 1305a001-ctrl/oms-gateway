"""Tests for the Prometheus exposition output of the cluster-exposure gauges."""
from __future__ import annotations

from oms_gateway.metrics import _escape_label, _Snapshot, render_metrics


def test_escape_label_handles_quotes_and_backslashes():
    assert _escape_label('foo"bar') == 'foo\\"bar'
    assert _escape_label("a\\b") == "a\\\\b"


def test_render_emits_help_and_type_lines():
    snap = _Snapshot()
    snap.by_bucket = {"poly-bet": 100.0}
    snap.by_cluster = {"poly:bitcoin": 50.0}
    out = render_metrics(snap)
    assert "# HELP oms_bucket_open_exposure_usd" in out
    assert "# TYPE oms_bucket_open_exposure_usd gauge" in out
    assert "# HELP oms_cluster_open_exposure_usd" in out


def test_render_includes_bucket_value():
    snap = _Snapshot()
    snap.by_bucket = {"poly-bet": 100.5}
    out = render_metrics(snap)
    assert 'oms_bucket_open_exposure_usd{bucket="poly-bet"} 100.50' in out


def test_render_includes_cluster_value():
    snap = _Snapshot()
    snap.by_cluster = {"poly:bitcoin": 730.25}
    out = render_metrics(snap)
    assert 'oms_cluster_open_exposure_usd{cluster="poly:bitcoin"} 730.25' in out


def test_render_utilization_when_below_cap():
    snap = _Snapshot()
    snap.by_cluster = {"poly:bitcoin": 400.0}
    out = render_metrics(snap)
    # Default cap = 8% × $10000 = $800. 400/800 = 0.5
    assert 'oms_cluster_exposure_utilization{cluster="poly:bitcoin"} 0.5' in out


def test_render_utilization_above_one_signals_breach():
    snap = _Snapshot()
    snap.by_cluster = {"poly:bitcoin": 900.0}
    out = render_metrics(snap)
    # 900 / 800 = 1.125
    assert 'oms_cluster_exposure_utilization{cluster="poly:bitcoin"} 1.1250' in out


def test_render_sorted_for_stable_output():
    snap = _Snapshot()
    snap.by_cluster = {"poly:zebra": 50.0, "poly:apple": 10.0}
    out = render_metrics(snap)
    apple_pos = out.find("apple")
    zebra_pos = out.find("zebra")
    assert apple_pos < zebra_pos


def test_render_handles_empty_snapshot():
    snap = _Snapshot()
    out = render_metrics(snap)
    # Cap-only cluster gauge still emits even with no clusters seen.
    assert "oms_cluster_exposure_cap_usd" in out
    # No utilization rows because no clusters.
    assert "oms_cluster_exposure_utilization{" not in out


def test_render_emits_cluster_cap_as_single_value():
    snap = _Snapshot()
    out = render_metrics(snap)
    # Cluster cap is global, not per-cluster.
    lines = [line for line in out.splitlines() if line.startswith("oms_cluster_exposure_cap_usd")]
    assert len(lines) == 1
    # Default 8% × $10000.
    assert "800.00" in lines[0]
