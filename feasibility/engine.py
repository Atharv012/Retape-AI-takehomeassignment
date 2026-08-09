from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import math

from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    default_first_payment_date,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
)


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


def round_half_up(val: float | int) -> int:
    return int(Decimal(str(val)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _get_floors(k: int, rules: CreditorRules) -> list[int]:
    """Compute minimum allowed creditor payment at each position 1..k."""
    floors = []
    prev = 0
    for i in range(1, k + 1):
        tier_floor = max(
            [min_c for from_pos, min_c in rules.min_payment_tiers if i >= from_pos],
            default=0,
        )
        base_min = rules.min_payment_cents
        if i > rules.max_token_pays:
            base_min += 1
        pos_floor = max(base_min, tier_floor, prev)
        floors.append(pos_floor)
        prev = pos_floor
    return floors


def _generate_candidate_payments(
    k: int, offer_total: int, rules: CreditorRules
) -> tuple[str, list[int]] | None:
    """Generate valid non-decreasing creditor payment amounts p_1..p_k satisfying rules."""
    floors = _get_floors(k, rules)

    if rules.even_pays:
        base = offer_total // k
        rem = offer_total % k
        # Remainder cents go to latest payments so sequence stays non-decreasing
        pays = [base] * (k - rem) + [base + 1] * rem
        # Validate constraints
        for i in range(k):
            if pays[i] < floors[i]:
                return None
        # Check token pay count limit
        token_count = sum(1 for p in pays if p == rules.min_payment_cents)
        if token_count > rules.max_token_pays:
            return None
        return ("even", pays)

    if rules.is_ballooning_allowed:
        # Front-load fee: early payments set to minimal floors
        if k == 1:
            pays = [offer_total]
        else:
            pays = list(floors[: k - 1])
            rem_bal = offer_total - sum(pays)
            pays.append(rem_bal)
        # Validate balloon final payment
        if pays[-1] < floors[-1] or (k > 1 and pays[-1] < pays[-2]):
            return None
        return ("balloon", pays)

    # Staircase case (neither even nor ballooning)
    # Generate minimal step-up staircase within max_segments distinct levels
    # We test segment partitions l_1..l_S with S <= max_segments
    best_pays = None

    def search_staircase(
        pos: int,
        segments_used: int,
        current_pays: list[int],
        last_val: int,
    ):
        nonlocal best_pays
        if best_pays is not None:
            return
        if pos == k:
            if sum(current_pays) == offer_total:
                best_pays = list(current_pays)
            return

        remaining_count = k - pos
        current_sum = sum(current_pays)
        needed_sum = offer_total - current_sum

        if needed_sum < remaining_count * max(floors[pos], last_val):
            return

        min_val = max(floors[pos], last_val)

        # Try continuing current segment value if valid
        if pos > 0 and min_val <= last_val:
            val = last_val
            max_possible = needed_sum - (remaining_count - 1) * val
            if val <= max_possible:
                search_staircase(pos + 1, segments_used, current_pays + [val], val)
                if best_pays is not None:
                    return

        # Try starting a new segment if segments_used < max_segments
        if segments_used < rules.max_segments:
            # We want to pick the smallest valid step-up value
            start_val = min_val if pos == 0 else max(min_val, last_val + 1)
            # Try a range of candidate step values
            step_candidates = [start_val]
            if remaining_count > 1:
                # Also try value that would evenly distribute remaining_sum
                avg_rem = (needed_sum) // remaining_count
                if avg_rem >= start_val:
                    step_candidates.append(avg_rem)
                    step_candidates.append(avg_rem + 1)

            for val in sorted(set(step_candidates)):
                if val < start_val:
                    continue
                max_possible = needed_sum - (remaining_count - 1) * val
                if val <= max_possible:
                    new_segs = segments_used + 1 if pos > 0 else 1
                    search_staircase(pos + 1, new_segs, current_pays + [val], val)
                    if best_pays is not None:
                        return

    # Fallback / heuristic staircase search: greedy minimal floors with step at segment boundaries
    # Try 1 to max_segments segments
    for num_segs in range(1, rules.max_segments + 1):
        if num_segs == 1:
            base = offer_total // k
            rem = offer_total % k
            pays = [base] * (k - rem) + [base + 1] * rem
            if all(pays[i] >= floors[i] for i in range(k)):
                token_count = sum(1 for p in pays if p == rules.min_payment_cents)
                if token_count <= rules.max_token_pays:
                    return ("staircase", pays)
        else:
            # Try segment split points
            # For 2 segments: split after m payments (1 <= m < k)
            if num_segs == 2:
                for m in range(1, k):
                    v1 = max(floors[m - 1], floors[0])
                    rem_total = offer_total - m * v1
                    rem_k = k - m
                    if rem_total <= 0:
                        continue
                    v2_base = rem_total // rem_k
                    v2_rem = rem_total % rem_k
                    if v2_base < v1:
                        continue
                    pays = [v1] * m + [v2_base] * (rem_k - v2_rem) + [v2_base + 1] * v2_rem
                    if all(pays[i] >= floors[i] for i in range(k)):
                        token_count = sum(1 for p in pays if p == rules.min_payment_cents)
                        if token_count <= rules.max_token_pays:
                            return ("staircase", pays)

    search_staircase(0, 0, [], 0)
    if best_pays is not None:
        return ("staircase", best_pays)

    return None


def _simulate_schedule(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    k: int,
    pay_shape: str,
    creditor_payments: list[int],
    extra_lump_cents: int = 0,
    extra_lump_date: date | None = None,
    extra_monthly_increment: int = 0,
) -> Result | None:
    first_pay_date = offer.first_payment_date or default_first_payment_date(client)
    cadence_dates = monthly_payment_dates(first_pay_date, k)

    if not cadence_dates or cadence_dates[-1] > client.last_draft_date:
        return None

    total_prog_fee = round_half_up(rules.program_fee_pct * offer.original_balance_cents)
    remaining_prog_fee = total_prog_fee

    # Extended cadence dates for fee-only months if needed
    all_fee_dates = list(cadence_dates)
    curr_fee_date = cadence_dates[-1]
    while remaining_prog_fee > 0 and curr_fee_date <= client.last_draft_date:
        # Add next EOM/clamped monthly date if we need more dates to collect fee
        next_dates = monthly_payment_dates(curr_fee_date, 2)
        if len(next_dates) < 2:
            break
        curr_fee_date = next_dates[1]
        if curr_fee_date <= client.last_draft_date:
            all_fee_dates.append(curr_fee_date)
        else:
            break

    # Build event timeline
    # Fixed credits and debits from client.ledger
    # Modify future drafts if extra_monthly_increment > 0
    # Add extra_lump_cents on extra_lump_date if specified
    events_by_date: dict[date, dict[str, list[int]]] = {}

    def add_evt(d: date, category: str, amount: int):
        if d not in events_by_date:
            events_by_date[d] = {"credit": [], "debit": []}
        events_by_date[d][category].append(amount)

    if extra_lump_cents > 0 and extra_lump_date is not None:
        add_evt(extra_lump_date, "credit", extra_lump_cents)

    for entry in client.ledger:
        if entry.date > client.as_of_date:
            amt = entry.amount_cents
            if entry.type == "credit" and extra_monthly_increment > 0:
                amt += extra_monthly_increment
            add_evt(entry.date, entry.type, amt)

    # Add creditor payments and bank fees on cadence_dates
    for idx, c_date in enumerate(cadence_dates):
        c_pay = creditor_payments[idx]
        b_fee = rules.bank_fee_cents
        add_evt(c_date, "debit", c_pay + b_fee)

    # Collect all unique dates in chronological order
    all_dates = sorted(set(events_by_date.keys()) | set(cadence_dates))
    if not all_dates:
        return None

    # Date-by-date simulation
    current_balance = client.current_balance_cents
    schedule_rows: list[ScheduleRow] = []

    for d in all_dates:
        # 1. Credits first (same-day ordering)
        credits = events_by_date.get(d, {}).get("credit", [])
        current_balance += sum(credits)

        # 2. Fixed debits & creditor/bank debits next
        debits = events_by_date.get(d, {}).get("debit", [])
        current_balance -= sum(debits)

        if current_balance < 0:
            return None  # Infeasible

        # 3. Collect program fee if this is a cadence date
        if d in all_fee_dates and remaining_prog_fee > 0:
            fee_to_take = min(remaining_prog_fee, current_balance)
            current_balance -= fee_to_take
            remaining_prog_fee -= fee_to_take
        else:
            fee_to_take = 0

        # Build schedule row if this is one of the creditor cadence dates
        if d in cadence_dates:
            idx = cadence_dates.index(d)
            schedule_rows.append(
                ScheduleRow(
                    date=d,
                    creditor_payment_cents=creditor_payments[idx],
                    program_fee_cents=fee_to_take,
                    bank_fee_cents=rules.bank_fee_cents,
                    balance_cents=current_balance,
                )
            )

    # Validate that full program fee was collected
    if remaining_prog_fee > 0:
        return None

    return Result(
        feasible=True,
        pay_shape_used=pay_shape,
        schedule=schedule_rows,
        additional_funds=None,
    )


def _evaluate_all_feasible(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_lump_cents: int = 0,
    extra_lump_date: date | None = None,
    extra_monthly_increment: int = 0,
) -> Result | None:
    offer_total = offer_total_cents(offer)
    max_k = min(rules.max_payments, rules.max_terms)

    first_pay_date = offer.first_payment_date or default_first_payment_date(client)

    best_result: Result | None = None

    for k in range(1, max_k + 1):
        cadence_dates = monthly_payment_dates(first_pay_date, k)
        if not cadence_dates or cadence_dates[-1] > client.last_draft_date:
            continue

        cand = _generate_candidate_payments(k, offer_total, rules)
        if cand is None:
            continue
        pay_shape, creditor_pays = cand

        res = _simulate_schedule(
            client,
            offer,
            rules,
            k,
            pay_shape,
            creditor_pays,
            extra_lump_cents=extra_lump_cents,
            extra_lump_date=extra_lump_date,
            extra_monthly_increment=extra_monthly_increment,
        )
        if res is not None:
            # We prefer solutions that front-load fee as early as possible
            if best_result is None:
                best_result = res

    return best_result


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification."""
    # 1. Try Part 1 (Feasible out-of-the-box)
    res = _evaluate_all_feasible(client, offer, rules)
    if res is not None:
        return res

    # 2. Part 2 (Infeasible offer -> compute minimum additional funds)
    future_drafts = [e for e in client.ledger if e.date > client.as_of_date and e.type == "credit"]
    num_future_drafts = len(future_drafts)

    # A. Monthly Increment (X)
    low = 1
    high = 10000000  # large upper bound
    best_inc = None

    while low <= high:
        mid = (low + high) // 2
        test_res = _evaluate_all_feasible(client, offer, rules, extra_monthly_increment=mid)
        if test_res is not None:
            best_inc = mid
            high = mid - 1
        else:
            low = mid + 1

    inc_cents = best_inc if best_inc is not None else 0
    max_inc_allowed = max(10000, round_half_up(0.40 * client.draft_amount_cents))
    inc_within_guardrail = inc_cents <= max_inc_allowed
    inc_reason = "" if inc_within_guardrail else f"Monthly increment {inc_cents} cents exceeds guardrail {max_inc_allowed} cents."

    monthly_inc_option = FundsOption(
        amount_cents=inc_cents,
        within_guardrail=inc_within_guardrail,
        reason=inc_reason,
        num_drafts=num_future_drafts,
    )

    # B. Lump Sum (L)
    # Target date for lump sum: first_draft_date or earliest future draft date
    lump_date = client.first_draft_date if client.first_draft_date > client.as_of_date else client.ledger[0].date

    low = 1
    high = 10000000
    best_lump = None

    while low <= high:
        mid = (low + high) // 2
        test_res = _evaluate_all_feasible(
            client, offer, rules, extra_lump_cents=mid, extra_lump_date=lump_date
        )
        if test_res is not None:
            best_lump = mid
            high = mid - 1
        else:
            low = mid + 1

    lump_cents = best_lump if best_lump is not None else 0
    offer_total = offer_total_cents(offer)
    max_lump_allowed = round_half_up(0.65 * offer_total)
    lump_within_guardrail = lump_cents <= max_lump_allowed
    lump_reason = "" if lump_within_guardrail else f"Lump sum {lump_cents} cents exceeds guardrail {max_lump_allowed} cents."

    lump_option = FundsOption(
        amount_cents=lump_cents,
        within_guardrail=lump_within_guardrail,
        reason=lump_reason,
        date=lump_date,
    )

    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(
            lump_sum=lump_option,
            monthly_increment=monthly_inc_option,
        ),
    )

