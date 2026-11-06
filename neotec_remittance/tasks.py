import frappe

from neotec_remittance.utils.timeutil import utc_now


def _safe(fn, *args):
	try:
		fn(*args)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Neotec Remittance scheduler: {fn.__module__}.{fn.__name__}")


def every_minute():
	from neotec_remittance.services import integrity, notify, outbox

	if not frappe.db.get_single_value("Remittance Settings", "enabled"):
		return
	_safe(outbox.process_due)
	_safe(integrity.seal_chain)
	_safe(notify.send_queued)


def every_five_minutes():
	from neotec_remittance.services import erp_bridge, ledger

	if not frappe.db.get_single_value("Remittance Settings", "enabled"):
		return
	_safe(erp_bridge.post_pending)
	_safe(ledger.refresh_deferred_balances)
	_safe(alert_unknown_payouts)


def daily():
	from neotec_remittance.services import erp_bridge, integrity

	_safe(integrity.verify_chain)
	_safe(erp_bridge.retry_errors)


def alert_unknown_payouts():
	from datetime import timedelta

	minutes = frappe.db.get_single_value("Remittance Settings", "unknown_alert_minutes") or 15
	cutoff = utc_now() - timedelta(minutes=minutes)
	rows = frappe.db.sql("""SELECT name, tracking_ref, payout_state, dispatched_at FROM `tabRemittance Transfer`
		WHERE payout_state IN ('Dispatching','Unknown','Recall Requested') AND dispatched_at < %s LIMIT 200""",
		cutoff, as_dict=True)
	fresh = []
	for r in rows:
		key = f"nr_unknown_alert:{r.name}"
		if not frappe.cache().get_value(key):
			frappe.cache().set_value(key, 1, expires_in_sec=6 * 3600)
			fresh.append(f"{r.tracking_ref} {r.payout_state} since {r.dispatched_at} UTC")
	if fresh:
		frappe.log_error(title=f"Neotec Remittance ALERT: {len(fresh)} payouts unresolved > {minutes} min",
			message="\n".join(fresh))
