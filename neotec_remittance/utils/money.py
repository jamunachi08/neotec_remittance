"""Exact money arithmetic. Floats are never used for monetary values."""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from neotec_remittance.utils.errors import ValidationFailed

# ISO 4217 minor-unit exponents that differ from 2.
EXPONENTS = {
	"BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
	"CLP": 0, "ISK": 0, "JPY": 0, "KRW": 0, "PYG": 0, "UGX": 0, "VND": 0, "XAF": 0, "XOF": 0,
}
RATE_PLACES = Decimal("0.000000000001")  # 12 dp


def D(value) -> Decimal:
	if isinstance(value, Decimal):
		return value
	if value is None or value == "":
		return Decimal(0)
	if isinstance(value, float):
		# Values read through Frappe documents arrive as floats; repr gives the shortest round-trip form.
		return Decimal(repr(value))
	try:
		return Decimal(str(value).strip())
	except (InvalidOperation, ValueError):
		raise ValidationFailed(f"Invalid decimal value: {value!r}")


def exponent(currency: str) -> int:
	return EXPONENTS.get((currency or "").upper(), 2)


def unit(currency: str) -> Decimal:
	return Decimal(1).scaleb(-exponent(currency))


def q(amount, currency: str, rounding=ROUND_HALF_UP) -> Decimal:
	return D(amount).quantize(unit(currency), rounding=rounding)


def q_down(amount, currency: str) -> Decimal:
	return q(amount, currency, ROUND_DOWN)


def is_exact(amount, currency: str) -> bool:
	a = D(amount)
	return a == a.quantize(unit(currency))


def amount_str(amount, currency: str) -> str:
	return format(q(amount, currency), "f")


def parse_rate(value, field="rate") -> Decimal:
	r = D(value)
	if r <= 0:
		raise ValidationFailed(f"{field} must be positive")
	if r != r.quantize(RATE_PLACES):
		raise ValidationFailed(f"{field} supports at most 12 decimal places")
	return r


def parse_percent(value, field="percent") -> Decimal:
	p = D(value or 0)
	if p < 0 or p >= 100:
		raise ValidationFailed(f"{field} must be between 0 and 100")
	return p


def positive_amount(value, currency: str, field="amount") -> Decimal:
	a = D(value)
	if a <= 0:
		raise ValidationFailed(f"{field} must be positive")
	if not is_exact(a, currency):
		raise ValidationFailed(f"{field} has more decimals than {currency} allows")
	return a
