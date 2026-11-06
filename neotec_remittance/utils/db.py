"""Low-level helpers for engine-owned tables.

Engine records (ledger, transfers, outbox, inbox...) are written with parameterised SQL so that
monetary values stay Decimal end to end and no document hook can intervene in the money path.
These DocTypes are read-only in Desk ("User Cannot Create" and no write permission)."""
import uuid

import frappe
from frappe.utils import now_datetime


def new_name() -> str:
	return uuid.uuid4().hex


def _user():
	try:
		return frappe.session.user or "Administrator"
	except Exception:
		return "Administrator"


def insert_row(doctype: str, values: dict, name: str | None = None) -> str:
	name = name or new_name()
	now = now_datetime()
	user = _user()
	row = {"name": name, "creation": now, "modified": now, "owner": user, "modified_by": user,
		"docstatus": 0, "idx": 0}
	row.update(values)
	cols = ", ".join(f"`{c}`" for c in row)
	placeholders = ", ".join(["%s"] * len(row))
	frappe.db.sql(f"INSERT INTO `tab{doctype}` ({cols}) VALUES ({placeholders})", tuple(row.values()))
	return name


def update_row(doctype: str, name: str, values: dict, where: dict | None = None) -> int:
	"""Conditional update; returns affected row count."""
	values = dict(values)
	values["modified"] = now_datetime()
	values["modified_by"] = _user()
	sets = ", ".join(f"`{k}`=%s" for k in values)
	params = list(values.values()) + [name]
	cond = ""
	for k, v in (where or {}).items():
		if isinstance(v, (list, tuple, set)):
			v = list(v)
			cond += f" AND `{k}` IN ({', '.join(['%s'] * len(v))})"
			params.extend(v)
		elif v is None:
			cond += f" AND `{k}` IS NULL"
		else:
			cond += f" AND `{k}`=%s"
			params.append(v)
	frappe.db.sql(f"UPDATE `tab{doctype}` SET {sets} WHERE name=%s{cond}", tuple(params))
	return affected_rows()


def affected_rows() -> int:
	cur = getattr(frappe.db, "_cursor", None)
	return int(cur.rowcount) if cur is not None else 0


def lock_row(doctype: str, name: str, fields="*"):
	rows = frappe.db.sql(f"SELECT {fields} FROM `tab{doctype}` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
	return rows[0] if rows else None


def get_row(doctype: str, name: str, fields="*"):
	rows = frappe.db.sql(f"SELECT {fields} FROM `tab{doctype}` WHERE name=%s", (name,), as_dict=True)
	return rows[0] if rows else None


def find_row(doctype: str, filters: dict, fields="*", for_update=False):
	cond = " AND ".join(f"`{k}`=%s" for k in filters)
	sql = f"SELECT {fields} FROM `tab{doctype}` WHERE {cond} LIMIT 1"
	if for_update:
		sql += " FOR UPDATE"
	rows = frappe.db.sql(sql, tuple(filters.values()), as_dict=True)
	return rows[0] if rows else None


def is_duplicate(exc) -> bool:
	try:
		if frappe.db.is_duplicate_entry(exc) or frappe.db.is_unique_key_violation(exc):
			return True
	except Exception:
		pass
	return "Duplicate entry" in str(exc)


def savepoint(label: str):
	frappe.db.savepoint(label)


def rollback_to(label: str):
	frappe.db.rollback(save_point=label)
