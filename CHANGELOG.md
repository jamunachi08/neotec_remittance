# Changelog

## 0.1.1 — 2026-11-06
- Fix: install/migrate no longer fails when the site has an unrelated broken field on Journal Entry
  (e.g. a Link to a DocType that does not exist). The export-id custom field is added without
  re-validating other customisations; any failure is logged instead of aborting.
- New: install/migrate reports broken Link/Table fields on Journal Entry in the Error Log and console.
- ERP bridge works with or without the export-id field (idempotency is held by Remittance ERP Posting);
  the export id is also written into the Journal Entry remark.

## 0.1.0 — 2026-11-05
Initial Release 1 foundation: subledger with hash chain and DB immutability triggers, multi-dimension
transfer state machine, durable outbox/inbox, idempotent customer API with gateway identity assertion,
quotes and corridors, versioned beneficiaries with envelope-encrypted accounts, funding receipts with
deterministic matching, internal watchlist screening with maker-checker compliance cases, liquidity
reservations, FX bridge accounting, partner charges, cancel/recall, refunds and returns, safeguarding
circuit breaker, ERPNext Journal Entry bridge, ZATCA per-transfer fee invoicing, notifications (EN/AR),
sandbox and gateway bank adapters.
