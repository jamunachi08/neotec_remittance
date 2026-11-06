"""Corridor selection, rate lookup and quote calculation. Accepted terms are immutable."""
from datetime import timedelta
from decimal import Decimal

import frappe

from neotec_remittance.services.settings import get_settings
from neotec_remittance.utils import db
from neotec_remittance.utils.errors import ConfigurationError, ValidationFailed
from neotec_remittance.utils.money import D, parse_percent, parse_rate, positive_amount, q, q_down
from neotec_remittance.utils.timeutil import to_dt, utc_now

HUNDRED = Decimal(100)


def select_corridor(entity, send_currency, receive_country, receive_currency, payout_method="Bank Account"):
	now = utc_now()
	rows = frappe.db.sql("""SELECT name FROM `tabRemittance Corridor`
		WHERE enabled=1 AND entity=%s AND send_currency=%s AND receive_country=%s AND receive_currency=%s
		AND payout_method=%s AND effective_from<=%s AND (effective_to IS NULL OR effective_to>%s)
		ORDER BY priority ASC, version DESC, name ASC""",
		(entity, send_currency, receive_country, receive_currency, payout_method, now, now), as_dict=True)
	if not rows:
		raise ValidationFailed("No approved corridor for this route", code="CORRIDOR_UNAVAILABLE")
	corridor = frappe.get_doc("Remittance Corridor", rows[0].name)
	partner_enabled = frappe.db.get_value("Remittance Partner", corridor.partner, "enabled")
	if not partner_enabled:
		raise ValidationFailed("Corridor partner is not available", code="CORRIDOR_UNAVAILABLE")
	return corridor


def current_rate(base, quote_currency):
	now = utc_now()
	rows = frappe.db.sql("""SELECT name, mid_rate, valid_from, stale_after_seconds FROM `tabRemittance Rate`
		WHERE base_currency=%s AND quote_currency=%s AND valid_from<=%s
		ORDER BY valid_from DESC, creation DESC LIMIT 1""", (base, quote_currency, now), as_dict=True)
	if not rows:
		raise ConfigurationError(f"No rate for {base}/{quote_currency}", code="RATE_UNAVAILABLE")
	r = rows[0]
	if to_dt(r.valid_from) + timedelta(seconds=r.stale_after_seconds or 900) < now:
		raise ConfigurationError(f"Rate {base}/{quote_currency} is stale", code="RATE_STALE")
	return r.name, parse_rate(r.mid_rate, "mid_rate")


def compute_fee(corridor, principal: Decimal) -> Decimal:
	cur = corridor.send_currency
	fee = D(corridor.fee_fixed) + principal * parse_percent(corridor.fee_percent, "fee_percent") / HUNDRED
	fee = max(fee, D(corridor.fee_min))
	if D(corridor.fee_max) > 0:
		fee = min(fee, D(corridor.fee_max))
	return q(fee, cur)


def calculate(corridor, principal: Decimal) -> dict:
	send, recv = corridor.send_currency, corridor.receive_currency
	if principal < D(corridor.min_amount) or principal > D(corridor.max_amount):
		raise ValidationFailed(f"Amount must be between {corridor.min_amount} and {corridor.max_amount} {send}",
			code="AMOUNT_OUT_OF_RANGE")
	if send == recv:
		rate_record, mid = None, Decimal(1)
		markup = Decimal(0)
	else:
		rate_record, mid = current_rate(send, recv)
		markup = parse_percent(corridor.fx_markup_percent, "fx_markup_percent")
	customer_rate = (mid * (HUNDRED - markup) / HUNDRED).quantize(Decimal("0.000000000001"))
	fee = compute_fee(corridor, principal)
	vat_rate = D(get_settings().vat_rate) if corridor.fee_vat_applicable else Decimal(0)
	vat = q(fee * vat_rate / HUNDRED, send)
	payout = q_down(principal * customer_rate, recv)
	if payout <= 0:
		raise ValidationFailed("Payout amount rounds to zero", code="AMOUNT_TOO_SMALL")
	return {"principal": principal, "fee": fee, "vat": vat, "total_payable": principal + fee + vat,
		"mid_rate": mid, "markup_percent": markup, "customer_rate": customer_rate, "payout_amount": payout,
		"rate_record": rate_record}


def create_quote(customer, send_amount, receive_country, receive_currency, payout_method="Bank Account") -> dict:
	send_currency = frappe.db.get_value("Company", customer.entity, "default_currency") or get_settings().functional_currency
	principal = positive_amount(send_amount, send_currency, "send_amount")
	corridor = select_corridor(customer.entity, send_currency, receive_country, receive_currency, payout_method)
	c = calculate(corridor, principal)
	now = utc_now()
	expires = now + timedelta(seconds=corridor.quote_ttl_seconds or 900)
	name = db.insert_row("Remittance Quote", {
		"customer": customer.name, "entity": customer.entity, "corridor": corridor.name,
		"corridor_version": corridor.version, "partner": corridor.partner, "status": "Open",
		"send_currency": send_currency, "receive_currency": receive_currency,
		"principal": c["principal"], "fee": c["fee"], "vat": c["vat"], "total_payable": c["total_payable"],
		"payout_amount": c["payout_amount"], "mid_rate": format(c["mid_rate"], "f"),
		"markup_percent": format(c["markup_percent"], "f"), "customer_rate": format(c["customer_rate"], "f"),
		"rate_record": c["rate_record"], "created_at": now, "expires_at": expires,
	})
	return quote_response(db.get_row("Remittance Quote", name), corridor)


def quote_response(qr, corridor=None) -> dict:
	from neotec_remittance.utils.money import amount_str
	from neotec_remittance.utils.timeutil import iso

	corridor = corridor or frappe.get_cached_doc("Remittance Corridor", qr.corridor)
	s, r = qr.send_currency, qr.receive_currency
	return {"quote_id": qr.name, "status": qr.status, "expires_at": iso(qr.expires_at),
		"send": {"currency": s, "principal": amount_str(qr.principal, s), "fee": amount_str(qr.fee, s),
			"vat": amount_str(qr.vat, s), "total": amount_str(qr.total_payable, s)},
		"receive": {"currency": r, "amount": amount_str(qr.payout_amount, r)},
		"rate": qr.customer_rate,
		"expected_delivery": {"en": corridor.expected_delivery_en, "ar": corridor.expected_delivery_ar}}
