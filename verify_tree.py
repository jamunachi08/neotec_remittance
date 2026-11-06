#!/usr/bin/env python3
"""Structural guard for neotec_remittance. Run before every commit: python3 verify_tree.py"""
import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
APP = ROOT / "neotec_remittance"
DT_DIR = APP / "neotec_remittance" / "doctype"
errors = []

REQUIRED = ["pyproject.toml", "README.md", "CHANGELOG.md", "license.txt", "neotec_remittance/__init__.py",
	"neotec_remittance/hooks.py", "neotec_remittance/modules.txt", "neotec_remittance/patches.txt",
	"neotec_remittance/install.py", "neotec_remittance/tasks.py", "neotec_remittance/translations/ar.csv"]
for f in REQUIRED:
	if not (ROOT / f).exists():
		errors.append(f"missing {f}")

version = re.search(r'__version__ = "([^"]+)"', (APP / "__init__.py").read_text()).group(1)
if f'app_version = "{version}"' not in (APP / "hooks.py").read_text():
	errors.append("hooks.app_version differs from __version__")
if f"## {version}" not in (ROOT / "CHANGELOG.md").read_text():
	errors.append(f"CHANGELOG has no entry for {version}")

names = set()
for jf in sorted(DT_DIR.glob("*/*.json")):
	d = json.loads(jf.read_text())
	folder = jf.parent.name
	if folder != d["name"].lower().replace(" ", "_") or jf.stem != folder:
		errors.append(f"{jf}: folder/name mismatch")
	if d.get("module") != "Neotec Remittance":
		errors.append(f"{jf}: wrong module")
	for req in ("__init__.py", f"{folder}.py"):
		if not (jf.parent / req).exists():
			errors.append(f"{jf.parent}: missing {req}")
	fieldnames = [f["fieldname"] for f in d["fields"]]
	if len(fieldnames) != len(set(fieldnames)):
		errors.append(f"{d['name']}: duplicate fieldnames")
	if d.get("field_order") and d["field_order"] != fieldnames:
		errors.append(f"{d['name']}: field_order out of sync")
	names.add(d["name"])
for jf in DT_DIR.glob("*/*.json"):
	d = json.loads(jf.read_text())
	for f in d["fields"]:
		if f["fieldtype"] in ("Link", "Table") and f.get("options", "").startswith("Remittance") and f["options"] not in names:
			errors.append(f"{d['name']}.{f['fieldname']}: link to unknown {f['options']}")

text_all = ""
for py in APP.rglob("*.py"):
	src = py.read_text()
	text_all += src
	try:
		ast.parse(src)
	except SyntaxError as e:
		errors.append(f"{py}: {e}")
	if "float(" in src and "/services/" in str(py):
		errors.append(f"{py}: float() in money services")
	if "alphax" in src.lower():
		errors.append(f"{py}: legacy brand name")

hooks = (APP / "hooks.py").read_text()
for dotted in re.findall(r'"(neotec_remittance\.[a-z_.]+)"', hooks):
	mod, _, attr = dotted.rpartition(".")
	path = ROOT / (mod.replace(".", "/") + ".py")
	if not path.exists() or f"def {attr}(" not in path.read_text():
		errors.append(f"hooks: {dotted} not found")

if errors:
	print("verify_tree FAILED:\n  " + "\n  ".join(errors))
	sys.exit(1)
print(f"verify_tree OK: {len(names)} doctypes, version {version}")
