"""All engine timestamps are naive UTC. Frappe's own creation/modified stay in system time."""
from datetime import date, datetime, timedelta, timezone


def utc_now() -> datetime:
	return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def utc_after(seconds: float) -> datetime:
	return utc_now() + timedelta(seconds=seconds)


def to_dt(value):
	if value is None or isinstance(value, datetime):
		return value
	if isinstance(value, date):
		return datetime(value.year, value.month, value.day)
	from frappe.utils import get_datetime

	return get_datetime(value)


def iso(value) -> str | None:
	value = to_dt(value)
	return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def riyadh_date(value=None) -> date:
	"""Accounting date in KSA (UTC+3, no DST)."""
	value = to_dt(value) or utc_now()
	return (value + timedelta(hours=3)).date()
