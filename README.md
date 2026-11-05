# Neotec Remittance

Remittance instruction engine, append-only subledger and bank execution layer for Frappe / ERPNext.
Neotec Integrated Solutions — version 0.1.0 (Release 1 foundation).

## Scope of Release 1
Verified individuals, bank-funded outbound remittance, bank-account payout, one legal entity, one collection
bank and one payout partner per corridor. Cash pickup, inbound, corporate bulk and wallets are out of scope.

## Architecture in one paragraph
Customer channels call a versioned API through an API gateway (gateway-asserted identity). The app owns
quotes, transfers, compliance decisions and an append-only double-entry subledger. Bank instructions are
written to a durable outbox in the same database transaction as the state change and executed by workers
that lease commands and never hold a transaction open across a network call. Bank evidence (callbacks,
status polls, dispatch responses) lands in a durable inbox and is applied exactly once under the transfer
lock. ERPNext receives one submitted Journal Entry per sealed subledger journal; fee VAT can be invoiced
per transfer for ZATCA.

## Invariants (tested)
- No duplicate monetary effect: unique event ids on journals, receipts, inbox events, payout attempts and
  idempotency scopes, enforced by database unique keys.
- No dispatch without every release guard re-checked under the transfer lock (KYC, compliance, fresh
  screening list, funding, cancellation, legal hold, partner/corridor, emergency stop).
- No resubmission after an uncertain outcome unless the partner guarantees duplicate prevention.
- No refund while payout execution is uncertain.
- No silent edits: engine records are refused as documents; ledger rows are protected by DB triggers
  (where the database user may create them) and by a SHA-256 hash chain verified daily.
- Money is Decimal end to end; amounts are checked against the currency's minor unit.

## Key DocTypes
| Area | DocTypes |
|---|---|
| Configuration | Remittance Settings, Remittance Partner, Remittance Corridor, Remittance Rate, Remittance Account |
| Parties | Remittance Customer, Remittance Beneficiary, Remittance Beneficiary Version, Remittance Watchlist Entry |
| Flow | Remittance Quote, Remittance Transfer, Remittance Funding Receipt, Remittance Payout Attempt, Remittance Reservation |
| Execution | Remittance Outbox, Remittance Inbox, Remittance Idempotency Record |
| Ledger | Remittance Journal, Remittance Ledger Line, Remittance Adjustment (maker-checker) |
| Controls | Remittance Compliance Case, Remittance Safeguarding Check, Remittance ERP Posting, Remittance Notification |

## Transfer state dimensions
`funding_state`, `compliance_state`, `payout_state`, `refund_state`, `recon_state` move independently;
`customer_status` is derived (mapping version `csm-1`) and never reveals screening detail.

## Public API (map these behind your gateway)
| Gateway route | Frappe method |
|---|---|
| POST /v1/quotes | `neotec_remittance.api.v1.quotes.create` |
| POST /v1/beneficiaries | `neotec_remittance.api.v1.beneficiaries.create` |
| GET /v1/beneficiaries | `neotec_remittance.api.v1.beneficiaries.list` |
| POST /v1/transfers (Idempotency-Key) | `neotec_remittance.api.v1.transfers.create` |
| GET /v1/transfers/{id} | `neotec_remittance.api.v1.transfers.get` |
| GET /v1/transfers | `neotec_remittance.api.v1.transfers.list` |
| POST /v1/transfers/{id}/cancel | `neotec_remittance.api.v1.transfers.cancel` |
| POST /v1/transfers/{id}/reconfirm | `neotec_remittance.api.v1.transfers.reconfirm` |
| GET /v1/receipts/{id} | `neotec_remittance.api.v1.transfers.receipt` |
| POST /callbacks/{partner} | `neotec_remittance.api.callbacks.receive?partner=` |

Responses: `{"data": ...}` or `{"error": {"code", "message"}}` with 201/200/202/401/403/404/409/422/503.

Gateway assertion headers: `X-Neotec-Subject`, `X-Neotec-Timestamp`,
`X-Neotec-Assertion = hex HMAC-SHA256(secret, "{ts}.{subject}.{METHOD}.{path}")`.
Partner callbacks: `X-Neotec-Timestamp`, `X-Neotec-Signature = hex HMAC-SHA256(secret, "{ts}." + raw_body)`.

## Setup checklist
1. Install: `bench get-app`, `bench --site <site> install-app neotec_remittance`.
2. Remittance Settings: default entity, identity mode + gateway secret, collection bank details,
   limits, EDD threshold, VAT rate, ZATCA mode.
3. Remittance Partner: adapter (Sandbox for UAT, Gateway for production), callback secret, capabilities
   exactly as certified with the bank.
4. Remittance Corridor (enable it — this seeds the subledger chart), Remittance Rate for the pair.
5. Map every Remittance Account to an ERPNext account; set `Collection Ledger Account`.
6. Load the watchlist (each change invalidates prior screening results).
7. Prefund the partner settlement asset with a Remittance Adjustment (Treasury Prefunding), approved by a
   second user.
8. Enable the platform and the scheduler.

## Operating notes
- **Emergency stop**: Settings → Controls → Emergency Stop. Pauses new dispatch only.
- **Unknown payouts** are polled per the status schedule and alerted after the configured minutes.
- **Quarantined inbox events** require investigation before re-queue.
- **Exception outbox commands**: payment submissions are never redriven; resolve with partner evidence.
- **Database triggers**: Settings shows whether they installed. On managed hosting without the TRIGGER
  privilege the hash chain is the tamper control; request the privilege for production.

## Integration hooks
```python
neotec_remittance_key_provider = "my_app.kms.Provider"           # KMS/HSM envelope key: key_id, wrap(), unwrap()
neotec_remittance_screening_providers = ["my_app.screen.run"]   # (transfer, subjects) -> [hit, ...]
neotec_remittance_verify_authorization = ["my_app.auth.verify"] # (customer, authorization_id, terms) -> bool
```

## Not included in 0.1.0
The bank gateway service (separate deployment holding mTLS keys and static egress), bank-specific
adapters, adapter-executed refunds, native mobile apps, inbound/cash-pickup products.

## Tests (see TEST_REPORT.md)
`bench --site <test-site> run-tests --app neotec_remittance` (requires a test site with ERPNext).
