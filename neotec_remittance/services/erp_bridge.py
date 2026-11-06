"""ERPNext bridge.

Each sealed subledger journal becomes one submitted ERPNext Journal Entry, created through supported
document operations (never direct GL rows), keyed by a unique export id (custom field on Journal Entry
plus the Remittance ERP Posting map). The subledger is never locked for sync: posting follows the sealed
chain order, so the ERP lag is a cutoff difference, not an error.

ZATCA mode "Per Transfer Invoice": each funded fee is invoiced as a submitted Sales Invoice (credited to the
Deferred Fee and VAT accounts, debited to the fee receivable). Journal lines on accounts flagged
`zatca_invoiced` are then mapped to the fee receivable so the fee and VAT are not booked twice. A fee
refund produces a credit note (Sales Invoice return). E-invoice clearance/reporting is handled by the
installed ZATCA app on submit."""
import frappe
from frappe.utils import flt

from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.money import D
from neotec_remittance.utils.timeutil import utc_now

EXPORT_FIELD = "neotec_remittance_export_id"


def post_pending(limit: int = 200):
	s = get_settings()
	if not s.erp_posting_enabled:
		return 0
	rows = frappe.db.sql("""SELECT name FROM `tabRemittance Journal` WHERE erp_status='Pending' AND sealed=1
		ORDER BY chain_seq LIMIT %s""", limit, as_dict=True)
	done = 0
	for r in rows:
		try:
			post_journal(r.name)
			frappe.db.commit()
			done += 1
		except Exception as e:
			frappe.db.rollback()
			frappe.db.sql("UPDATE `tabRemittance Journal` SET erp_status='Error' WHERE name=%s", r.name)
			frappe.log_error(title=f"Neotec Remittance: ERP posting failed for {r.name}", message=str(e)[:1000])
			frappe.db.commit()
	if s.zatca_mode == "Per Transfer Invoice":
		post_fee_documents()
	return done


def retry_errors():
	frappe.db.sql("UPDATE `tabRemittance Journal` SET erp_status='Pending' WHERE erp_status='Error'")
	frappe.db.commit()


def post_journal(journal_name: str):
	s = get_settings()
	j = db.get_row("Remittance Journal", journal_name)
	export_id = f"nrj:{j.name}"
	if frappe.db.exists("Remittance ERP Posting", {"export_id": export_id}):
		frappe.db.sql("UPDATE `tabRemittance Journal` SET erp_status='Posted' WHERE name=%s", j.name)
		return
	company_currency = frappe.get_cached_value("Company", j.entity, "default_currency")
	rate = D(1) if j.currency == company_currency else _rate(j.currency, company_currency, j.accounting_date)
	lines = frappe.db.sql("""SELECT l.account, l.debit, l.credit, a.erp_account, a.zatca_invoiced
		FROM `tabRemittance Ledger Line` l JOIN `tabRemittance Account` a ON a.name=l.account
		WHERE l.journal=%s ORDER BY l.line_no""", j.name, as_dict=True)
	accounts = []
	for l in lines:
		erp_account, party = l.erp_account, None
		if s.zatca_mode == "Per Transfer Invoice" and l.zatca_invoiced:
			erp_account, party = s.fee_receivable_account, s.fee_customer
		if not erp_account:
			raise frappe.ValidationError(f"Remittance Account {l.account} has no ERPNext account mapping")
		row = {"account": erp_account, "debit_in_account_currency": flt(l.debit), "credit_in_account_currency": flt(l.credit),
			"exchange_rate": flt(rate), "cost_center": s.erp_cost_center,
			"user_remark": f"{j.event_type} {j.event_id}"[:140]}
		if party:
			row.update({"party_type": "Customer", "party": party})
		accounts.append(row)
	values = {"doctype": "Journal Entry", "voucher_type": "Journal Entry", "company": j.entity,
		"posting_date": j.accounting_date, "multi_currency": 1 if j.currency != company_currency else 0,
		"user_remark": f"Neotec Remittance {j.event_type} {j.event_id} [{export_id}]"[:500], "accounts": accounts}
	if frappe.get_meta("Journal Entry").has_field(EXPORT_FIELD):
		values[EXPORT_FIELD] = export_id
	je = frappe.get_doc(values)
	je.flags.ignore_permissions = True
	je.insert()
	_round_off(je, s)
	je.submit()
	db.insert_row("Remittance ERP Posting", {"export_id": export_id, "kind": "Journal", "journal": j.name,
		"transfer": j.transfer, "status": "Posted", "journal_entry": je.name})
	frappe.db.sql("UPDATE `tabRemittance Journal` SET erp_status='Posted', erp_reference=%s WHERE name=%s", (je.name, j.name))


def _round_off(je, s):
	diff = flt(je.total_debit) - flt(je.total_credit)
	if abs(diff) < 0.005:
		return
	if not s.erp_round_off_account or abs(diff) > 1:
		raise frappe.ValidationError(f"Journal Entry out of balance by {diff} after currency translation")
	je.append("accounts", {"account": s.erp_round_off_account, "cost_center": s.erp_cost_center,
		"debit_in_account_currency": -diff if diff < 0 else 0, "credit_in_account_currency": diff if diff > 0 else 0,
		"exchange_rate": 1, "user_remark": "Neotec Remittance translation rounding"})
	je.save()


def _rate(from_currency, to_currency, date):
	rows = frappe.db.sql("""SELECT mid_rate FROM `tabRemittance Rate` WHERE base_currency=%s AND quote_currency=%s
		AND DATE(valid_from)<=%s ORDER BY valid_from DESC LIMIT 1""", (to_currency, from_currency, date))
	if rows:
		return D(1) / D(rows[0][0])
	rows = frappe.db.sql("""SELECT mid_rate FROM `tabRemittance Rate` WHERE base_currency=%s AND quote_currency=%s
		AND DATE(valid_from)<=%s ORDER BY valid_from DESC LIMIT 1""", (from_currency, to_currency, date))
	if rows:
		return D(rows[0][0])
	from erpnext.setup.utils import get_exchange_rate

	r = get_exchange_rate(from_currency, to_currency, date)
	if not r:
		raise frappe.ValidationError(f"No translation rate {from_currency}->{to_currency} for {date}")
	return D(str(r))


def post_fee_documents(limit: int = 200):
	"""ZATCA: one fee invoice per funded transfer; a credit note when the fee is refunded."""
	s = get_settings()
	if not (s.fee_item and s.fee_customer and s.fee_receivable_account):
		frappe.log_error(title="Neotec Remittance: ZATCA fee invoicing is not configured")
		return
	todo = frappe.db.sql("""SELECT t.name FROM `tabRemittance Transfer` t
		LEFT JOIN `tabRemittance ERP Posting` p ON p.export_id=CONCAT('fee_inv:', t.name)
		WHERE t.funding_state='Funded' AND t.fee>0 AND p.name IS NULL ORDER BY t.funded_at LIMIT %s""", limit, as_dict=True)
	for r in todo:
		_fee_doc(r.name, is_return=False)
	todo = frappe.db.sql("""SELECT t.name FROM `tabRemittance Transfer` t
		JOIN `tabRemittance ERP Posting` inv ON inv.export_id=CONCAT('fee_inv:', t.name) AND inv.status='Posted'
		LEFT JOIN `tabRemittance ERP Posting` p ON p.export_id=CONCAT('fee_cn:', t.name)
		JOIN `tabRemittance Journal` j ON j.event_id=CONCAT('refund_payable:', t.name)
		WHERE p.name IS NULL AND EXISTS (SELECT 1 FROM `tabRemittance Ledger Line` l WHERE l.journal=j.name
			AND l.account_role='Deferred Fee' AND l.debit>0) LIMIT %s""", limit, as_dict=True)
	for r in todo:
		_fee_doc(r.name, is_return=True)


def _fee_doc(transfer_name, is_return):
	s = get_settings()
	export_id = ("fee_cn:" if is_return else "fee_inv:") + transfer_name
	try:
		t = db.get_row("Remittance Transfer", transfer_name)
		income = frappe.db.get_value("Remittance Account", {"resolution_key": f"{t.entity}|{t.send_currency}|Deferred Fee|-"},
			"erp_account")
		si = frappe.get_doc({"doctype": "Sales Invoice", "company": t.entity, "customer": s.fee_customer,
			"currency": t.send_currency, "posting_date": frappe.utils.getdate(), "set_posting_time": 1,
			"debit_to": s.fee_receivable_account, "taxes_and_charges": s.fee_taxes_template,
			"remarks": f"Remittance fee {t.tracking_ref}", "is_return": 1 if is_return else 0,
			"items": [{"item_code": s.fee_item, "qty": -1 if is_return else 1, "rate": flt(t.fee),
				"income_account": income, "cost_center": s.erp_cost_center}]})
		if is_return:
			si.return_against = frappe.db.get_value("Remittance ERP Posting", {"export_id": "fee_inv:" + transfer_name}, "sales_invoice")
		if s.fee_taxes_template:
			si.set_taxes()
		si.flags.ignore_permissions = True
		si.insert()
		if abs(abs(flt(si.total_taxes_and_charges)) - flt(t.vat)) > 0.009:
			raise frappe.ValidationError(f"ERPNext VAT {si.total_taxes_and_charges} differs from subledger VAT {t.vat}")
		si.submit()
		db.insert_row("Remittance ERP Posting", {"export_id": export_id, "kind": "Fee Credit Note" if is_return else "Fee Invoice",
			"transfer": transfer_name, "status": "Posted", "sales_invoice": si.name})
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title=f"Neotec Remittance: fee document failed {export_id}", message=str(e)[:1000])
