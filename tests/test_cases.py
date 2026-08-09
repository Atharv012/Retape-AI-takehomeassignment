"""Example expectations for the four provided cases.

These FAIL until you implement feasibility/engine.py::evaluate_offer. Treat them
as the minimum bar — your own test suite should go well beyond these. They do not
pin an exact schedule (several valid schedules may exist); they assert the
verdict, the pay shape, and the Part 2 minima.
"""

from __future__ import annotations

from datetime import date

import pytest

from feasibility.engine import evaluate_offer
from feasibility.models import load_case


def _run(case: str):
    client, offer, rules = load_case(f"cases/{case}")
    return evaluate_offer(client, offer, rules)


def test_case1_feasible_even():
    r = _run("case1_feasible_even")
    assert r.feasible is True
    assert r.pay_shape_used == "even"
    assert r.schedule is not None
    # balance must never go negative
    assert all(row.balance_cents >= 0 for row in r.schedule)
    # Check exact sum matching offer total (50000 cents)
    total_paid = sum(row.creditor_payment_cents for row in r.schedule)
    assert total_paid == 50000
    # Check total fee collected equals program fee (30000 cents)
    total_fee = sum(row.program_fee_cents for row in r.schedule)
    assert total_fee == 30000


def test_case2_infeasible_minima():
    r = _run("case2_infeasible_minima")
    assert r.feasible is False
    af = r.additional_funds
    assert af is not None
    assert af.lump_sum.amount_cents == 10000
    assert af.lump_sum.within_guardrail is True
    assert af.monthly_increment.amount_cents == 2500
    assert af.monthly_increment.num_drafts == 5
    assert af.monthly_increment.within_guardrail is True


def test_case3_requires_balloon():
    r = _run("case3_balloon")
    assert r.feasible is True
    # this creditor allows ballooning; the solver defers payment into a final balloon
    assert r.pay_shape_used == "balloon"
    assert r.schedule is not None
    # Verify balloon shape: final payment is much larger than earlier payments
    payments = [row.creditor_payment_cents for row in r.schedule]
    assert len(payments) > 1
    assert payments[-1] > payments[0]
    assert sum(payments) == 30000  # offer total (60000 * 0.5)


def test_case4_tiered_minimums():
    r = _run("case4_tiers")
    assert r.feasible is True
    assert r.pay_shape_used == "staircase"
    # payments 7+ must respect the $50 tier floor
    payments = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
    assert all(p >= 5000 for p in payments[6:])
    assert sum(payments) == 60000  # offer total (150000 * 0.4)


def test_guardrails_validation():
    # Test high infeasible requirement that triggers guardrail violation
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    # Increase creditor balance significantly to drive up lump sum and monthly increment
    offer.current_balance_cents = 500000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    assert r.additional_funds is not None
    # Lump sum guardrail limit: 65% of 250,000 = 162,500
    # Monthly increment limit: max(10000, 0.40 * 10000) = 10000
    assert r.additional_funds.monthly_increment.within_guardrail is False
    assert "exceeds guardrail" in r.additional_funds.monthly_increment.reason


def test_horizon_limit():
    client, offer, rules = load_case("cases/case1_feasible_even")
    # Set horizon before first payment date
    client.last_draft_date = date(2026, 1, 15)
    r = evaluate_offer(client, offer, rules)
    # Should be infeasible as payment dates exceed horizon
    assert r.feasible is False

