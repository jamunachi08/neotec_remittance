# Neotec Remittance 0.1.0 — Build & Test Evidence

Environment: Frappe v15 + ERPNext v15, MariaDB 10.11, Python 3.12 (Ubuntu 24.04), 5 Nov 2026.

## Automated suite — `bench --site <site> run-tests --app neotec_remittance`
34 tests, 34 passed, 0 skipped (repeated runs on the same site also pass).

| Spec | Test | Result |
|---|---|---|
| T01 | Same Idempotency-Key + same body returns the same transfer | Pass |
| T02 | Same key + different body → IDEMPOTENCY_CONFLICT, no second transfer | Pass |
| T03 | Bank executes but submission times out → Unknown → status poll → Paid; one submission, one payout journal | Pass |
| T04 | Duplicate and out-of-order callbacks → one journal; stale Accepted ignored after Paid | Pass |
| T05 | Cancel before dispatch → no outbox command, refund pending; cancel after dispatch → recall, refund blocked while uncertain | Pass |
| T06 | Concurrent reservations never exceed partner liquidity | Pass |
| T07 | Expired KYC and stale screening list block every release path | Pass |
| T09 | Paid then returned → original history kept, return journal, refund through Finance | Pass |
| T10 | Duplicate bank entry → single financial effect | Pass |
| T11 | Worker dies after claiming a payment command → never resent; resolved by status inquiry | Pass |
| T12 | Cross-customer transfer and beneficiary access denied (service and HTTP layers) | Pass |
| T13 | Callback signature: tampered body, stale timestamp, wrong key rejected | Pass |
| T15 | ERP mapping unavailable → subledger intact; backlog posts exactly once after fix | Pass |
| — | Quote arithmetic exact (fee, 15% VAT, FX markup, round-down payout) | Pass |
| — | Ledger rejects imbalance, sub-unit precision, conflicting replay | Pass |
| — | Engine records refuse Desk/REST edits and deletes | Pass |
| — | DB trigger blocks UPDATE on ledger lines | Pass |
| — | Hash chain seals and verifies | Pass |
| — | Safeguarding shortfall pauses dispatch | Pass |
| — | Screening hit → Hold → maker-checker (self-approval refused) → reject → refund | Pass |
| — | Never-executed uncertain payout → certified not-found → reversal + refund | Pass |
| — | NOT_SENT retried safely with backoff | Pass |
| — | Two workers cannot lease the same command | Pass |
| — | Partial payment, overpayment and excess refund | Pass |
| — | Late funding → reconfirmation → release | Pass |
| — | 24-hour limit enforced | Pass |
| — | IBAN check digits; account encrypted at rest, masked in views | Pass |
| — | ERPNext Journal Entry posted once, submitted, with export id | Pass |

T08 (cash-agent redemption) belongs to Release 3; T14 (regional failover) is an operational drill.

## HTTP smoke test (gunicorn, gateway-assertion identity)
quote 201 · beneficiary 201 · transfer 201 · replay 200 (same id) · conflict 409 · missing key 422 ·
get 200 (EN/AR status) · bad assertion 401 · other customer 404 · list 200 · cancel 202 ·
signed callback 200 (committed to inbox) · tampered callback 401 · guest REST on Remittance Transfer 403.

## Not yet verified
- Frappe v16 (needs Python 3.14; code avoids v15-only APIs but has not been run on v16).
- ZATCA per-transfer fee invoice / credit note path (code complete; needs a configured fee item,
  VAT template and the ZATCA app in UAT).
- True multi-process races (the locking/conditional-update design is exercised sequentially).
- Frappe Cloud privileges for CREATE TRIGGER (Settings shows the result after install).
