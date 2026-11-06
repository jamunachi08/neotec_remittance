"""Sanctions/watchlist screening and compliance decisions.

Providers are pluggable through the hook `neotec_remittance_screening_providers` (callables returning a
list of hit dicts). The built-in provider checks the internal watchlist. A stale or missing list blocks
release; it never bypasses screening. Customer-facing status never reveals screening detail."""
import json

import frappe

from neotec_remittance.services import execution, refunds, state
from neotec_remittance.services.audit import note
from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import AccessDenied, GuardFailed
from neotec_remittance.utils.money import D
from neotec_remittance.utils.names import name_tokens, normalize_name
from neotec_remittance.utils.timeutil import utc_now

RULE_VERSION = "screen-1"


def internal_watchlist(subjects: list[str]) -> list[dict]:
	hits = []
	for subject in filter(None, subjects):
		norm = normalize_name(subject)
		tokens = name_tokens(subject)
		if not tokens:
			continue
		probe = max(tokens, key=len)
		for e in frappe.db.sql("""SELECT name, full_name, name_normalized, list_source, list_version
				FROM `tabRemittance Watchlist Entry` WHERE active=1 AND name_normalized LIKE %s LIMIT 500""",
				(f"%{probe}%",), as_dict=True):
			et = name_tokens(e.name_normalized)
			if e.name_normalized == norm or (len(et) >= 2 and et <= tokens):
				hits.append({"subject": subject, "entry": e.name, "listed_name": e.full_name, "source": e.list_source,
					"list_version": e.list_version, "match": "exact" if e.name_normalized == norm else "token-subset"})
	return hits


def screen(t):
	"""Caller holds the transfer lock. Updates compliance_state in place."""
	s = get_settings()
	if not execution.screening_fresh(frappe._dict(t, screened_at=utc_now(), screening_list_version=s.screening_list_version), s):
		state.save(t, {"last_error_code": "SCREENING_LIST_STALE"})
		return t.compliance_state
	bv = db.get_row("Remittance Beneficiary Version", t.beneficiary_version, "full_name, full_name_ar")
	cust = db.get_row("Remittance Customer", t.customer, "customer_name, customer_name_ar, risk_tier")
	subjects = [bv.full_name, bv.full_name_ar, cust.customer_name, cust.customer_name_ar]
	hits = internal_watchlist(subjects)
	for path in frappe.get_hooks("neotec_remittance_screening_providers") or []:
		hits.extend(frappe.get_attr(path)(t, subjects) or [])
	stamp = {"screened_at": utc_now(), "screening_list_version": s.screening_list_version, "last_error_code": None}
	edd_tiers = {x.strip() for x in (s.edd_risk_tiers or "").split(",") if x.strip()}
	needs_edd = D(t.principal) >= D(s.edd_threshold or 0) > 0 or cust.risk_tier in edd_tiers

	if hits:
		target, case_type = "Hold", "Screening Hit"
	elif needs_edd and t.compliance_state != "Approved":
		target, case_type = "Pending EDD", "Enhanced Due Diligence"
	else:
		target, case_type = "Approved", None

	if case_type:
		open_case = frappe.db.get_value("Remittance Compliance Case", {"transfer": t.name, "status": ("!=", "Decided")})
		if not open_case:
			case = frappe.get_doc({"doctype": "Remittance Compliance Case", "transfer": t.name, "customer": t.customer,
				"case_type": case_type, "status": "Open", "rule_version": RULE_VERSION, "opened_at": utc_now(),
				"screening_evidence": json.dumps({"hits": hits, "edd": bool(needs_edd)}, indent=1, ensure_ascii=False)})
			case.flags.engine = True
			case.insert(ignore_permissions=True)
			stamp["compliance_case"] = case.name
	if t.compliance_state == target:
		state.save(t, stamp)
	else:
		state.save(t, stamp, {"compliance_state": target})
	return t.compliance_state


def apply_decision(case):
	t = state.lock_transfer(case.transfer)
	if t.compliance_state not in ("Hold", "Pending EDD"):
		raise GuardFailed(f"Transfer compliance state is {t.compliance_state}")
	if case.decision == "Approve":
		state.save(t, {"screened_at": utc_now(), "screening_list_version": get_settings().screening_list_version},
			{"compliance_state": "Approved"})
		note("Remittance Transfer", t.name, f"Compliance approved (case {case.name})")
		frappe.db.after_commit.add(lambda: _release_after_commit(t.name))
		return
	hold = case.decision == "Reject and Hold Funds"
	transitions = {"compliance_state": "Rejected"}
	if t.payout_state in ("Not Ready", "Ready"):
		execution.release_reservation(t)
		transitions["payout_state"] = "Cancelled"
	state.save(t, {"legal_hold": 1 if hold else t.legal_hold, "closed_at": utc_now()}, transitions)
	note("Remittance Transfer", t.name, f"Compliance rejected (case {case.name}){' with legal hold' if hold else ''}")
	if not hold and D(t.funded_amount) > 0:
		refunds.initiate(t, "compliance rejection")


def _release_after_commit(name):
	try:
		execution.try_release(name)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Neotec Remittance: release after approval failed for {name}")


def on_watchlist_change():
	"""A list change invalidates every prior screening result for transfers not yet dispatched."""
	version = frappe.generate_hash(length=12)
	from neotec_remittance.services.settings import set_values

	set_values({"screening_list_updated_at": utc_now(), "screening_list_version": version})
	try:
		frappe.enqueue("neotec_remittance.services.execution.release_due", queue="long", job_id="nr_rescreen",
			deduplicate=True, enqueue_after_commit=True)
	except Exception:
		pass  # the 2-minute release sweep re-screens anyway
