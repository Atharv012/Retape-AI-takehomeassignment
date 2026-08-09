# Settlement Feasibility & Fee Engine — Take-home

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests (pass out of the box)
│   └── test_cases.py        # example expectations — make these pass, then add your own
├── run.py                   # python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q
```

Out of the box, `tests/test_smoke.py` passes and `tests/test_cases.py` fails —
the latter is your target. Go beyond those four cases with your own tests.

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.

---

## Solution Notes & Design Documentation

### 1. Approach & Alternatives Considered

- **Chronological Date-by-Date Ledger Simulator**: We model all events (`credit`, `debit`, `creditor_payment`, `bank_fee`, `program_fee`) on an exact calendar timeline. On any given date, credits are applied first (same-day ordering), followed by debits, ensuring that the account balance never drops below zero.
- **Explicit Rounding (`round_half_up`)**: Python's native `round()` uses round-half-to-even. To satisfy §3 hard constraints, all percentage and fraction calculations (`offer_total`, `program_fee`, guardrail bounds) explicitly use `ROUND_HALF_UP` via Python's `decimal` module.
- **Front-Loading Fee Optimization**: On each cadence date, after accounting for creditor payments and bank fees, the simulator greedily collects as much of the remaining program fee as the running balance allows (fee collected = minimum of remaining fee and available balance). If the fee is not fully collected by the final payment date, fee collection extends onto subsequent monthly cadence dates (as fee-only dates without bank fees) up to `last_draft_date`.
- **Infeasible Minima Optimization**: For infeasible offers (Part 2), we use binary search to determine the minimum uniform **Monthly Increment** $X$ (added to future drafts) and minimum **Lump Sum** $L$ (placed on the earliest future draft date). Both options evaluate guardrail boundaries and output detailed reasons when exceeded.

### 2. Interpretation of Payment Shapes

- **Even (`"even"`)**: Active when `even_pays = True`. Calculates base payment as `floor(offer_total / k)` and distributes remainder cents (+1 cent) to the latest payments in the sequence to ensure a strict non-decreasing order (p1 ≤ p2 ≤ ... ≤ pk).
- **Balloon (`"balloon"`)**: Active when `is_ballooning_allowed = True` and `even_pays = False`. To maximize fee collection early, early payments p1 through p(k-1) sit exactly at position floor minimums (the maximum of base minimums, tier step-ups, and token pay caps), while the final payment pk absorbs the remaining balance.
- **Staircase (`"staircase"`)**: Active when neither `even_pays` nor `is_ballooning_allowed` is set. Constrained by `max_segments`. Early payment segments remain minimal to maximize early fee collection, stepping up to higher levels as required to reach `offer_total`.

### 3. Assumptions & Edge Cases Handled

- **Same-day Ordering**: Credits land before debits on every date.
- **Token Pays & Tier Interaction**: Position floors enforce creditor payment minimums: the first few payments can be nominal "token" amounts (`min_payment_cents`), but subsequent payments after `max_token_pays` must step up (be at least `min_payment_cents + 1 cent`).
- **Horizon Boundary**: Payment schedules strictly validate that no cadence dates exceed `last_draft_date`.