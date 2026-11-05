"""Transfer state dimensions, allowed transitions and the derived customer status.

Frappe docstatus is not used. Each dimension moves independently under the transfer row lock, and every
write is a conditional UPDATE on row_version, so a racing writer gets ConcurrencyConflict, never a lost update."""
import frappe

from neotec_remittance.utils import db
from neotec_remittance.utils.errors import ConcurrencyConflict, InvalidTransition, NotFound

TRANSITIONS = {
	"funding_state": {
		"Awaiting Funds": {"Partially Funded", "Funded", "Expired"},
		"Partially Funded": {"Funded", "Expired"},
		"Funded": set(), "Expired": set(),
	},
	"compliance_state": {
		"Pending": {"Approved", "Hold", "Pending EDD", "Rejected"},
		"Hold": {"Approved", "Rejected"},
		"Pending EDD": {"Approved", "Rejected", "Hold"},
		"Approved": {"Hold", "Pending EDD"},  # re-screening after a list update
		"Rejected": set(),
	},
	"payout_state": {
		"Not Ready": {"Ready", "Cancelled"},
		"Ready": {"Not Ready", "Dispatching", "Cancelled"},
		"Dispatching": {"Accepted", "Unknown", "Paid", "Rejected"},
		"Accepted": {"Paid", "Unknown", "Rejected", "Recall Requested"},
		"Unknown": {"Accepted", "Paid", "Rejected", "Recall Requested"},
		"Recall Requested": {"Recalled", "Paid", "Accepted", "Rejected"},
		"Paid": {"Returned"},
		"Rejected": set(), "Recalled": set(), "Returned": set(), "Cancelled": set(),
	},
	"refund_state": {
		"None": {"Eligible"},
		"Eligible": {"Pending"},
		"Pending": {"Refunded", "Eligible"},
		"Refunded": set(),
	},
	"recon_state": {
		"Unreconciled": {"Matched", "Exception"},
		"Exception": {"Matched"},
		"Matched": {"Exception"},
	},
}
UNCERTAIN_PAYOUT = {"Dispatching", "Accepted", "Unknown", "Recall Requested"}
PAYOUT_TERMINAL_NO_DELIVERY = {"Rejected", "Recalled", "Cancelled"}
CUSTOMER_STATUS_MAPPING_VERSION = "csm-1"

CUSTOMER_STATUS_AR = {
	"Awaiting Payment": "بانتظار الدفع", "Partial Payment Received": "تم استلام دفعة جزئية",
	"Action Required": "مطلوب إجراء", "Under Review": "قيد المراجعة", "Processing": "قيد المعالجة",
	"Sent to Partner": "أُرسلت إلى الشريك", "Delivered": "تم التسليم", "Returned": "مُرتجعة",
	"Cancelled": "ملغاة", "Refund in Progress": "الاسترداد قيد التنفيذ", "Refunded": "تم الاسترداد",
	"Expired": "منتهية", "Unable to Process": "تعذّرت المعالجة",
}


def derive_customer_status(t) -> str:
	"""Customer-facing summary. Never reveals screening or fraud detail (tipping-off control)."""
	if t.refund_state == "Refunded":
		return "Refunded"
	if t.payout_state == "Paid":
		return "Delivered"
	if t.payout_state == "Returned":
		return "Returned"
	if t.refund_state in ("Eligible", "Pending"):
		return "Refund in Progress"
	if t.compliance_state == "Rejected":
		return "Unable to Process"
	if t.payout_state == "Cancelled":
		return "Cancelled"
	if t.funding_state == "Expired":
		return "Expired"
	if t.requote_required:
		return "Action Required"
	if t.payout_state in UNCERTAIN_PAYOUT:
		return "Sent to Partner"
	if t.funding_state == "Awaiting Funds":
		return "Awaiting Payment"
	if t.funding_state == "Partially Funded":
		return "Partial Payment Received"
	if t.compliance_state in ("Hold", "Pending EDD"):
		return "Under Review"
	return "Processing"


def lock_transfer(name: str):
	t = db.lock_row("Remittance Transfer", name)
	if not t:
		raise NotFound(f"Transfer {name} not found")
	return t


def check(t, dimension: str, target: str):
	current = t.get(dimension)
	if target == current:
		return False
	if target not in TRANSITIONS[dimension].get(current, set()):
		raise InvalidTransition(f"{dimension}: {current} -> {target} is not allowed",
			details={"dimension": dimension, "from": current, "to": target})
	return True


def save(t, changes: dict, transitions: dict | None = None):
	"""Apply validated transitions plus field changes atomically against the locked row version."""
	for dimension, target in (transitions or {}).items():
		if check(t, dimension, target):
			changes[dimension] = target
	if not changes:
		return t
	preview = frappe._dict(t)
	preview.update(changes)
	changes["customer_status"] = derive_customer_status(preview)
	new_version = (t.row_version or 0) + 1
	changes["row_version"] = new_version
	n = db.update_row("Remittance Transfer", t.name, changes, where={"row_version": t.row_version or 0})
	if n != 1:
		raise ConcurrencyConflict(f"Transfer {t.name} changed concurrently")
	t.update(changes)
	return t
