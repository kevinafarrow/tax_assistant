from dataclasses import dataclass
from pathlib import Path

from tax_assistant import costs, db


@dataclass
class FakeUsage:
    input_tokens: int = 2000
    output_tokens: int = 300
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeConfig:
    data_dir: Path
    claude_balance_usd: float | None = 25.0
    claude_balance_as_of: str = ""


def test_estimate_opus_pricing():
    # 2000 in @ $5/M + 300 out @ $25/M = $0.01 + $0.0075
    cost = costs.estimate_cost_usd("claude-opus-4-8", FakeUsage())
    assert cost == 0.0175


def test_estimate_prefers_longest_prefix():
    # claude-sonnet-5 must match its own row, not claude-sonnet-4's
    cost = costs.estimate_cost_usd("claude-sonnet-5", FakeUsage(input_tokens=1_000_000,
                                                               output_tokens=0))
    assert cost == 3.0


def test_estimate_unknown_model_returns_none():
    assert costs.estimate_cost_usd("claude-2.1", FakeUsage()) is None


def test_estimate_includes_cache_tokens():
    usage = FakeUsage(input_tokens=0, output_tokens=0,
                      cache_creation_input_tokens=1_000_000,
                      cache_read_input_tokens=1_000_000)
    # opus input $5/M: write 1.25x = $6.25, read 0.1x = $0.50
    assert costs.estimate_cost_usd("claude-opus-4-8", usage) == 6.75


def test_record_call_and_remaining_balance(tmp_path):
    cfg = FakeConfig(data_dir=tmp_path)
    cost = costs.record_call(cfg, "claude-opus-4-8", FakeUsage())
    assert cost == 0.0175
    assert costs.remaining_balance_usd(cfg) == 25.0 - 0.0175


def test_remaining_balance_respects_anchor_date(tmp_path):
    cfg = FakeConfig(data_dir=tmp_path)
    costs.record_call(cfg, "claude-opus-4-8", FakeUsage())
    # Anchor in the far future: nothing recorded counts against it yet
    future = FakeConfig(data_dir=tmp_path, claude_balance_as_of="2999-01-01")
    assert costs.remaining_balance_usd(future) == 25.0


def test_remaining_balance_unconfigured(tmp_path):
    cfg = FakeConfig(data_dir=tmp_path, claude_balance_usd=None)
    assert costs.remaining_balance_usd(cfg) is None


def _insert_call(tmp_path, cost_usd, created_at):
    with db.connect(tmp_path) as conn:
        conn.execute(
            """INSERT INTO api_calls (model, input_tokens, output_tokens, cost_usd, created_at)
               VALUES ('m', 0, 0, ?, ?)""",
            (cost_usd, created_at),
        )


def test_sync_balance_supersedes_env_anchor(tmp_path):
    cfg = FakeConfig(data_dir=tmp_path, claude_balance_usd=100.0,
                     claude_balance_as_of="2026-01-01")
    _insert_call(tmp_path, 2.0, "2026-02-01T00:00:00+00:00")
    assert costs.remaining_balance_usd(cfg) == 98.0

    costs.sync_balance(cfg, 50.0)  # newer than the .env anchor → wins
    assert costs.remaining_balance_usd(cfg) == 50.0  # old spend no longer counted

    _insert_call(tmp_path, 3.0, "2999-01-01T00:00:00+00:00")  # after the sync
    assert costs.remaining_balance_usd(cfg) == 47.0
    assert costs.lifetime_spend_usd(cfg) == 5.0  # lifetime ignores anchors


def test_env_anchor_wins_when_newer(tmp_path):
    with db.connect(tmp_path) as conn:
        conn.execute("INSERT INTO balance_anchors (balance_usd, noted_at) "
                     "VALUES (10.0, '2026-01-01T00:00:00+00:00')")
    cfg = FakeConfig(data_dir=tmp_path, claude_balance_usd=99.0,
                     claude_balance_as_of="2026-06-01")
    assert costs.remaining_balance_usd(cfg) == 99.0


def test_summary(tmp_path):
    cfg = FakeConfig(data_dir=tmp_path, claude_balance_usd=None)
    _insert_call(tmp_path, 1.5, "2026-02-01T00:00:00+00:00")
    s = costs.summary(cfg)
    assert s["calls"] == 1 and s["lifetime_usd"] == 1.5
    assert s["anchor_usd"] is None and s["remaining_usd"] is None

    costs.sync_balance(cfg, 20.0)
    s = costs.summary(cfg)
    assert s["anchor_usd"] == 20.0
    assert s["spent_since_anchor_usd"] == 0.0
    assert s["remaining_usd"] == 20.0
    assert s["lifetime_usd"] == 1.5


def test_unknown_model_recorded_without_cost(tmp_path):
    cfg = FakeConfig(data_dir=tmp_path)
    assert costs.record_call(cfg, "mystery-model", FakeUsage()) is None
    with db.connect(tmp_path) as conn:
        row = conn.execute("SELECT * FROM api_calls").fetchone()
    assert row["model"] == "mystery-model" and row["cost_usd"] is None
    # NULL costs must not poison the spend sum
    assert costs.remaining_balance_usd(cfg) == 25.0
