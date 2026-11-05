"""Customer notifications go through their own durable queue so a provider outage never blocks money flow."""
import frappe

from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.money import amount_str

TEMPLATES = {
	"transfer_accepted": {
		"en": ("Transfer {ref} created", "Your transfer {ref} of {total} {cur} is awaiting payment. Use reference {fref}."),
		"ar": ("تم إنشاء الحوالة {ref}", "حوالتك {ref} بمبلغ {total} {cur} بانتظار الدفع. استخدم المرجع {fref}."),
	},
	"transfer_funded": {
		"en": ("Payment received for {ref}", "We received your payment for transfer {ref}."),
		"ar": ("تم استلام الدفعة للحوالة {ref}", "استلمنا دفعتك للحوالة {ref}."),
	},
	"transfer_paid": {
		"en": ("Transfer {ref} delivered", "Transfer {ref} was delivered: {payout} {pcur}."),
		"ar": ("تم تسليم الحوالة {ref}", "تم تسليم الحوالة {ref}: {payout} {pcur}."),
	},
	"action_required": {
		"en": ("Action required for {ref}", "Please review the updated terms for transfer {ref} in the app."),
		"ar": ("مطلوب إجراء للحوالة {ref}", "يرجى مراجعة الشروط المحدثة للحوالة {ref} في التطبيق."),
	},
	"refund_completed": {
		"en": ("Refund completed for {ref}", "Your refund for transfer {ref} has been sent."),
		"ar": ("تم الاسترداد للحوالة {ref}", "تم إرسال المبلغ المسترد للحوالة {ref}."),
	},
}


def queue(transfer, template: str):
	s = get_settings()
	cust = frappe.db.get_value("Remittance Customer", transfer.customer, ["email", "mobile_no", "language"], as_dict=True)
	if not cust:
		return
	channels = []
	if s.notify_email and cust.email:
		channels.append(("Email", cust.email))
	if s.notify_sms and cust.mobile_no:
		channels.append(("SMS", cust.mobile_no))
	for channel, recipient in channels:
		db.insert_row("Remittance Notification", {"transfer": transfer.name, "customer": transfer.customer,
			"template": template, "channel": channel, "language": cust.language or "ar", "recipient": recipient,
			"status": "Queued", "attempts": 0})


def render(n):
	t = db.get_row("Remittance Transfer", n.transfer)
	lang = n.language if n.language in ("en", "ar") else "ar"
	subject, body = TEMPLATES[n.template][lang]
	ctx = {"ref": t.tracking_ref, "total": amount_str(t.total_payable, t.send_currency), "cur": t.send_currency,
		"fref": t.funding_reference, "payout": amount_str(t.payout_amount, t.receive_currency), "pcur": t.receive_currency}
	return subject.format(**ctx), body.format(**ctx)


def send_queued(limit=200):
	rows = frappe.db.sql("""SELECT * FROM `tabRemittance Notification` WHERE status='Queued' AND attempts<5
		ORDER BY creation LIMIT %s""", limit, as_dict=True)
	for n in rows:
		try:
			subject, body = render(n)
			if n.channel == "Email":
				frappe.sendmail(recipients=[n.recipient], subject=subject, message=body, delayed=True)
			else:
				from frappe.core.doctype.sms_settings.sms_settings import send_sms

				send_sms([n.recipient], body, success_msg=False)
			db.update_row("Remittance Notification", n.name, {"status": "Sent", "attempts": n.attempts + 1})
		except Exception as e:
			db.update_row("Remittance Notification", n.name, {"attempts": n.attempts + 1, "error": str(e)[:300],
				"status": "Failed" if n.attempts + 1 >= 5 else "Queued"})
		frappe.db.commit()
