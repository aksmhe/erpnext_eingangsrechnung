"""
Eingangsrechnungen aus einem Ordner in ERPNext übernehmen.

Ablauf:
1. pruefen()     – findet PDFs, die noch nicht nach dem Schema benannt sind, und
                   schlägt Datum / Lieferant / Rechnungsnummer vor.
2. umbenennen()  – benennt die vom Benutzer bestätigten Dateien um
                   (oder merkt sie als "keine Rechnung").
3. erfassen()    – legt für jede fertig benannte PDF einen ENTWURF einer
                   Eingangsrechnung mit PDF-Anhang an und verschiebt bezahlte
                   Rechnungen aus dem Ordner für offene Rechnungen.

Gebucht (Submit) wird nie automatisch. Es werden keine externen Dienste benutzt.
"""

import hashlib
import json
import re
import shutil
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime

from erpnext_eingangsrechnung import erkennung as e
from erpnext_eingangsrechnung import lieferant as lf

EINST = "Eingangsrechnung Import Einstellungen"
LOG = "Eingangsrechnung Datei"


class LieferantFehlt(Exception):
	"""Lieferant ist in ERPNext nicht vorhanden; enthält einen Vorschlag zum Anlegen."""

	def __init__(self, name, vorschlag):
		super().__init__(_("Lieferant '{0}' nicht gefunden.").format(name))
		self.name = name
		self.vorschlag = vorschlag


RECHTSFORM_WOERTER = {"gmbh", "mbh", "gbr", "kg", "ohg", "ug", "ag", "partg", "co", "und", "the", "ltd", "inc"}


# ---------------------------------------------------------------- Hilfen

def _darf():
	if not frappe.has_permission("Purchase Invoice", "create"):
		frappe.throw(_("Keine Berechtigung zum Anlegen von Eingangsrechnungen."), frappe.PermissionError)


def _einstellungen():
	return frappe.get_single(EINST)


def _ordner(einst):
	if not einst.ordner:
		frappe.throw(_("In den Einstellungen ist kein Rechnungsordner eingetragen."))
	ordner = Path(einst.ordner)
	if not ordner.is_dir():
		frappe.throw(_("Der Rechnungsordner {0} ist nicht erreichbar.").format(einst.ordner))
	return ordner


def _sicherer_pfad(ordner, rel):
	"""Relativer Pfad aus dem Browser -> echte Datei, nur innerhalb des Ordners."""
	teile = [t for t in str(rel or "").split("/") if t]
	if not teile or any(t == ".." or e.ausgelassen(t) for t in teile):
		frappe.throw(_("Ungültiger Dateipfad."))
	pfad = ordner.joinpath(*teile)
	try:
		pfad.resolve().relative_to(ordner.resolve())
	except ValueError:
		frappe.throw(_("Ungültiger Dateipfad."))
	if pfad.suffix.lower() != ".pdf" or not pfad.is_file():
		frappe.throw(_("Datei nicht gefunden: {0}").format(rel))
	return pfad


def _rel(ordner, pfad):
	return Path(pfad).relative_to(ordner).as_posix()


def _hash(pfad):
	h = hashlib.sha256()
	with open(pfad, "rb") as datei:
		for block in iter(lambda: datei.read(1024 * 1024), b""):
			h.update(block)
	return h.hexdigest()


def _suche_datei(ordner, name):
	direkt = ordner / name
	if direkt.is_file():
		return direkt
	treffer = sorted(
		(p for p in ordner.rglob(name) if not any(e.ausgelassen(t) for t in p.relative_to(ordner).parts)),
		key=lambda p: (len(p.parts), str(p)),
	)
	return treffer[0] if treffer else None


def _regeln(einst, ordner):
	if (einst.lieferanten_liste or "").strip():
		bekannte, eigene = e.lade_lieferanten(zeilen=einst.lieferanten_liste.splitlines())
	else:
		bekannte, eigene = e.lade_lieferanten(_suche_datei(ordner, "lieferanten.txt"))
	# Die eigene Firma steht auf jeder Rechnung -> nie als Lieferant nehmen
	if einst.firma:
		for wort in re.findall(r"\w{5,}", einst.firma):
			if wort.lower() not in RECHTSFORM_WOERTER:
				eigene.append(wort.lower())
	return bekannte, eigene


def _zahler(einst):
	zahler = [{"kuerzel": z.kuerzel, "bezeichnung": z.bezeichnung or z.kuerzel, "konto": z.konto}
		for z in (einst.zahler or [])]
	if not zahler:
		# Rückfall: gesellschafter.txt aus dem Rechnungsordner (nur Kürzel, ohne Konto)
		try:
			datei = _suche_datei(Path(einst.ordner), "gesellschafter.txt")
			zahler = [{"kuerzel": g["kuerzel"], "bezeichnung": g["name"], "konto": None}
				for g in e.lade_gesellschafter(datei)]
		except Exception:
			zahler = []
	return zahler


def _log_eintraege():
	zeilen = frappe.get_all(LOG, fields=["name", "pfad", "sha256", "groesse", "status", "rechnung", "lieferant"])
	return {z.sha256: z for z in zeilen}, {z.pfad: z for z in zeilen}


def _log_setzen(name=None, **werte):
	if name:
		frappe.db.set_value(LOG, name, werte)
		return name
	doc = frappe.get_doc({"doctype": LOG, **werte})
	doc.insert(ignore_permissions=True)
	return doc.name


# ---------------------------------------------------------------- 1. Prüfen

@frappe.whitelist()
def pruefen(manuell=0):
	"""Liste der PDFs, die noch umbenannt werden müssen (mit Vorschlägen)."""
	_darf()
	einst = _einstellungen()
	if not einst.aktiv:
		return {"aktiv": 0, "fehler": _("Der Ordner-Import ist in den Einstellungen nicht aktiv.")}
	if not cint(manuell) and not einst.beim_oeffnen_pruefen:
		return {"aktiv": 0}
	try:
		ordner = _ordner(einst)
	except frappe.ValidationError as fehler:
		frappe.clear_last_message()
		return {"aktiv": 1, "fehler": str(fehler)}

	bekannte, eigene = _regeln(einst, ordner)
	nach_hash, nach_pfad = _log_eintraege()
	cache = frappe.cache()
	liste = []
	for pfad in e.pdf_dateien(ordner):
		if e.ZIELMUSTER.match(pfad.name) or e.KEINE_RECHNUNG.search(pfad.name):
			continue
		rel = _rel(ordner, pfad)
		st = pfad.stat()
		bekannt = nach_pfad.get(rel)
		if bekannt and bekannt.status == "Ignoriert" and bekannt.groesse == st.st_size:
			continue
		schluessel = f"erpnext_eingangsrechnung|v2|{rel}|{st.st_mtime}|{st.st_size}"
		info = cache.get_value(schluessel)
		if info is None:
			info = e.analysiere(pfad, bekannte, eigene)
			info["sha256"] = _hash(pfad)
			cache.set_value(schluessel, info, expires_in_sec=7 * 24 * 3600)
		if info["sha256"] in nach_hash and nach_hash[info["sha256"]].status == "Ignoriert":
			continue
		eintrag = {
			"pfad": rel,
			"datei": pfad.name,
			"ordner": str(Path(rel).parent) if "/" in rel else "",
			"art": "",
			"datum": info.get("datum") or "",
			"lieferant": info.get("lieferant") or "",
			"nummer": info.get("nummer") or "",
			"betrag": info.get("betrag"),
			"quelle": info.get("quelle") or "",
			"grund": info.get("grund") or "",
		}
		beleg = info.get("paypal")
		if beleg:
			# Zahlungsbeleg: wird nach der Rechnung benannt, zu der er gehört
			rechnung = _rechnung_zum_beleg(einst, ordner, beleg, None, info["sha256"]) if einst.firma else None
			bill_no = frappe.db.get_value("Purchase Invoice", rechnung, "bill_no") if rechnung else None
			eintrag.update({
				"art": "paypal",
				"datum": beleg.get("datum") or eintrag["datum"],
				"lieferant": beleg.get("verkaeufer") or eintrag["lieferant"],
				"nummer": e.saeubern(bill_no or beleg.get("rechnungsnummer") or beleg.get("transaktionscode") or ""),
				"betrag": beleg.get("betrag_eur") or beleg.get("betrag"),
				"grund": (_("PayPal-Zahlungsbeleg zu Rechnung {0} – keine eigene Rechnung.").format(rechnung)
					if rechnung else _("PayPal-Zahlungsbeleg – keine eigene Rechnung. Passende Rechnung noch nicht gefunden.")),
			})
		liste.append(eintrag)
	return {
		"aktiv": 1,
		"umbenennen": liste,
		"zahler": [{"kuerzel": z["kuerzel"], "bezeichnung": z["bezeichnung"]} for z in _zahler(einst)],
	}


# ---------------------------------------------------------------- 2. Umbenennen

@frappe.whitelist(methods=["POST"])
def umbenennen(auftraege):
	"""auftraege: [{pfad, aktion: umbenennen|ignorieren|ueberspringen, datum, lieferant, nummer, kuerzel}]"""
	_darf()
	einst = _einstellungen()
	ordner = _ordner(einst)
	if isinstance(auftraege, str):
		auftraege = json.loads(auftraege)
	erlaubte_kuerzel = {z["kuerzel"] for z in _zahler(einst)}
	ergebnis = []
	for a in auftraege or []:
		aktion = a.get("aktion")
		if aktion not in ("umbenennen", "ignorieren"):
			continue
		try:
			pfad = _sicherer_pfad(ordner, a.get("pfad"))
		except frappe.ValidationError as fehler:
			frappe.clear_last_message()
			ergebnis.append({"pfad": a.get("pfad"), "ok": 0, "meldung": str(fehler)})
			continue

		if aktion == "ignorieren":
			h = _hash(pfad)
			vorhanden = frappe.db.get_value(LOG, {"sha256": h})
			_log_setzen(vorhanden, pfad=_rel(ordner, pfad), sha256=h, groesse=pfad.stat().st_size,
				status="Ignoriert", hinweis=_("Vom Benutzer als 'keine Rechnung' markiert."))
			ergebnis.append({"pfad": a.get("pfad"), "ok": 1, "meldung": _("ignoriert")})
			continue

		datum = (a.get("datum") or "").strip()
		lieferant = e.saeubern(a.get("lieferant") or "")
		nummer = e.saeubern(a.get("nummer") or "")
		kuerzel = (a.get("kuerzel") or "").strip()
		if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", datum) or not lieferant or not nummer:
			ergebnis.append({"pfad": a.get("pfad"), "ok": 0,
				"meldung": _("Datum (JJJJ-MM-TT), Lieferant und Rechnungsnummer sind nötig.")})
			continue
		if kuerzel and kuerzel not in erlaubte_kuerzel:
			ergebnis.append({"pfad": a.get("pfad"), "ok": 0, "meldung": _("Unbekanntes Kürzel {0}.").format(kuerzel)})
			continue
		namensfunktion = e.belegname if a.get("art") == "paypal" else e.zielname
		ziel = e.freier_name(pfad.parent, namensfunktion(datum, lieferant, nummer, kuerzel))
		try:
			pfad.rename(ziel)
		except OSError as fehler:
			ergebnis.append({"pfad": a.get("pfad"), "ok": 0,
				"meldung": _("Umbenennen fehlgeschlagen: {0}").format(fehler.strerror)})
			continue
		ergebnis.append({"pfad": a.get("pfad"), "ok": 1, "neu": _rel(ordner, ziel)})
	return ergebnis


# ---------------------------------------------------------------- 3. Erfassen

@frappe.whitelist(methods=["POST"])
def erfassen():
	_darf()
	return _erfassen(_einstellungen())


def stuendlich():
	einst = _einstellungen()
	if not (einst.aktiv and einst.stuendlich):
		return
	try:
		_erfassen(einst)
	except Exception:
		frappe.log_error(title="Eingangsrechnung Import: stündlicher Lauf")


def _erfassen(einst):
	ordner = _ordner(einst)
	if not einst.firma:
		frappe.throw(_("Bitte in den Einstellungen die Firma eintragen."))
	bekannte, eigene = _regeln(einst, ordner)
	zahler = _zahler(einst)
	kuerzel_liste = [z["kuerzel"] for z in zahler]
	nach_hash, nach_pfad = _log_eintraege()

	bericht = {"angelegt": [], "vorhanden": [], "verschoben": [], "belege": [], "fehler": [], "lieferant_fehlt": []}
	fehlende = {}
	for pfad in e.pdf_dateien(ordner):
		ist_beleg = e.ist_belegname(pfad.name)
		if not ist_beleg and (not e.ZIELMUSTER.match(pfad.name) or e.KEINE_RECHNUNG.search(pfad.name)):
			continue
		rel = _rel(ordner, pfad)
		groesse = pfad.stat().st_size
		eintrag = nach_pfad.get(rel)
		if not (eintrag and eintrag.groesse == groesse):
			h = _hash(pfad)
			eintrag = nach_hash.get(h)
			if eintrag and eintrag.pfad != rel:
				# Datei wurde verschoben oder umbenannt -> Pfad nachziehen
				frappe.db.set_value(LOG, eintrag.name, {"pfad": rel, "groesse": groesse})
				eintrag.pfad = rel
			if not eintrag:
				eintrag = frappe._dict(name=None, sha256=h, status=None, rechnung=None, lieferant=None)
		if ist_beleg or eintrag.status == "Zahlungsbeleg":
			# Zahlungsbeleg: nur der Rechnung zuordnen, nie als Rechnung anlegen
			if not eintrag.rechnung:
				try:
					beleg = e.paypal_beleg(e.pdf_lesen(pfad)[0])
					if beleg:
						_beleg_verarbeiten(einst, ordner, pfad, rel, eintrag, beleg, kuerzel_liste, bericht)
				except Exception as fehler:
					frappe.db.rollback()
					frappe.clear_last_message()
					bericht["fehler"].append({"datei": rel, "meldung": _("Zahlungsbeleg: {0}").format(fehler)})
			frappe.db.commit()
			continue
		if eintrag.status == "Ignoriert":
			continue
		if not eintrag.rechnung:
			try:
				eintrag = _neue_datei(einst, ordner, pfad, rel, eintrag, bekannte, eigene, kuerzel_liste, bericht)
			except Exception as fehler:
				frappe.db.rollback()
				frappe.clear_last_message()
				meldung = str(fehler) or fehler.__class__.__name__
				if isinstance(fehler, LieferantFehlt):
					eintrag_fehlt = fehlende.get(fehler.name)
					if not eintrag_fehlt:
						eintrag_fehlt = {"name": fehler.name, "vorschlag": fehler.vorschlag, "dateien": []}
						fehlende[fehler.name] = eintrag_fehlt
						bericht["lieferant_fehlt"].append(eintrag_fehlt)
					eintrag_fehlt["dateien"].append(rel)
					meldung = _("Lieferant '{0}' fehlt in ERPNext.").format(fehler.name)
				bericht["fehler"].append({"datei": rel, "meldung": meldung})
				try:
					_log_setzen(eintrag.name or frappe.db.get_value(LOG, {"sha256": eintrag.sha256}),
						pfad=rel, sha256=eintrag.sha256, groesse=groesse, status="Fehler", hinweis=meldung[:500])
				except Exception:
					pass
				frappe.db.commit()
				continue
		if einst.verschieben and eintrag and eintrag.rechnung and eintrag.status != "Zahlungsbeleg":
			try:
				_verschieben_wenn_bezahlt(einst, ordner, pfad, rel, eintrag, zahler, kuerzel_liste, bericht)
			except Exception as fehler:
				frappe.clear_last_message()
				bericht["fehler"].append({"datei": rel, "meldung": _("Verschieben: {0}").format(fehler)})
		frappe.db.commit()

	zusammenfassung = _("{0} Entwurf/Entwürfe angelegt, {1} vorhanden, {2} verschoben, {3} Zahlungsbeleg(e), {4} Fehler").format(
		len(bericht["angelegt"]), len(bericht["vorhanden"]), len(bericht["verschoben"]), len(bericht["belege"]),
		len(bericht["fehler"]))
	frappe.db.set_single_value(EINST, {"letzter_lauf": now_datetime(), "letztes_ergebnis": zusammenfassung})
	frappe.db.commit()
	bericht["zusammenfassung"] = zusammenfassung
	return bericht


# ---------------------------------------------------------------- Lieferant / Rechnung finden

def _finde_lieferant(name, ust_ids):
	"""Name aus dem Dateinamen (+ USt-IdNr. aus dem PDF) -> Supplier."""
	if ust_ids:
		treffer = frappe.get_all("Supplier", filters={"tax_id": ["in", list(set(ust_ids))], "disabled": 0}, pluck="name")
		if len(treffer) == 1:
			return treffer[0]
	if not name:
		return None
	for filt in (
		{"supplier_name": name},
		{"name": name},
		{"supplier_name": ["like", f"{name}%"]},
		{"supplier_name": ["like", f"%{name}%"]},
	):
		treffer = frappe.get_all("Supplier", filters={**filt, "disabled": 0}, pluck="name", limit=2)
		if len(treffer) == 1:
			return treffer[0]
	erstes_wort = re.findall(r"\w{4,}", name)
	if erstes_wort:
		treffer = frappe.get_all("Supplier", filters={"supplier_name": ["like", f"{erstes_wort[0]}%"], "disabled": 0},
			pluck="name", limit=2)
		if len(treffer) == 1:
			return treffer[0]
	return None


def _finde_rechnung(firma, lieferant, nummer):
	"""Vorhandene (nicht stornierte) Eingangsrechnung mit gleicher Rechnungsnummer."""
	schluessel = e.nummer_schluessel(nummer)
	if not schluessel:
		return None
	filters = {"company": firma, "docstatus": ["<", 2], "bill_no": ["is", "set"]}
	if lieferant:
		filters["supplier"] = lieferant
	for r in frappe.get_all("Purchase Invoice", filters=filters, fields=["name", "bill_no"], order_by="creation desc"):
		if e.nummer_schluessel(r.bill_no) == schluessel:
			return r.name
	return None


def _hat_anhang(rechnung):
	return frappe.db.exists("File", {"attached_to_doctype": "Purchase Invoice", "attached_to_name": rechnung})


def _anhaengen(rechnung, pfad):
	datei = frappe.get_doc({
		"doctype": "File",
		"file_name": pfad.name,
		"attached_to_doctype": "Purchase Invoice",
		"attached_to_name": rechnung,
		"is_private": 1,
		"content": pfad.read_bytes(),
	})
	datei.save(ignore_permissions=True)


def _vorlage(firma, lieferant):
	"""Aufwandskonto/Text/Kostenstelle von der letzten Rechnung dieses Lieferanten."""
	letzte = frappe.get_all("Purchase Invoice",
		filters={"company": firma, "supplier": lieferant, "docstatus": ["<", 2]},
		fields=["name"], order_by="docstatus desc, posting_date desc, creation desc", limit=1)
	if letzte:
		zeile = frappe.get_all("Purchase Invoice Item", filters={"parent": letzte[0].name},
			fields=["item_name", "expense_account", "cost_center"], order_by="idx asc", limit=1)
		if zeile and zeile[0].expense_account:
			return zeile[0]
	return frappe._dict(
		item_name=None,
		expense_account=frappe.get_cached_value("Company", firma, "default_expense_account"),
		cost_center=frappe.get_cached_value("Company", firma, "cost_center"),
	)


# ---------------------------------------------------------------- Entwurf anlegen

def _neue_datei(einst, ordner, pfad, rel, eintrag, bekannte, eigene, kuerzel_liste, bericht):
	teile = e.zerlege_namen(pfad.name, kuerzel_liste)
	if not teile:
		raise frappe.ValidationError(_("Dateiname passt nicht zum Schema."))
	info = e.analysiere(pfad, bekannte, eigene)
	if info.get("paypal"):
		# PayPal-Zahlungsbeleg, obwohl als Rechnung benannt -> zuordnen statt anlegen
		_beleg_verarbeiten(einst, ordner, pfad, rel, eintrag, info["paypal"], kuerzel_liste, bericht)
		return frappe._dict(name=eintrag.name, sha256=eintrag.sha256, status="Zahlungsbeleg", rechnung=None, pfad=rel)
	lieferant = eintrag.get("lieferant") or _alias(teile["lieferant"]) or _finde_lieferant(
		teile["lieferant"], info.get("ust_ids"))
	vorschlag = None
	if not lieferant:
		vorschlag = _vorschlag(einst, pfad, teile, info, eigene)
		if vorschlag.get("tax_id"):
			lieferant = _finde_lieferant(None, [vorschlag["tax_id"]])

	# Originale Rechnungsnummer (z. B. mit "/") aus der E-Rechnung bevorzugen
	nummer = teile["nummer"]
	if info.get("nummer_roh") and e.nummer_schluessel(info["nummer_roh"]) == e.nummer_schluessel(nummer):
		nummer = info["nummer_roh"]

	vorhanden = _finde_rechnung(einst.firma, lieferant, nummer)
	if vorhanden:
		hinweis = _("Rechnung war schon in ERPNext.")
		if einst.pdf_an_vorhandene and not _hat_anhang(vorhanden):
			_anhaengen(vorhanden, pfad)
			hinweis += " " + _("PDF angehängt.")
		name = _log_setzen(eintrag.name, pfad=rel, sha256=eintrag.sha256, groesse=pfad.stat().st_size,
			status="Vorhanden", rechnung=vorhanden, hinweis=hinweis, lieferant=lieferant)
		bericht["vorhanden"].append({"datei": rel, "rechnung": vorhanden, "hinweis": hinweis})
		return frappe._dict(name=name, sha256=eintrag.sha256, status="Vorhanden", rechnung=vorhanden, pfad=rel)

	if not lieferant:
		raise LieferantFehlt(teile["lieferant"], vorschlag)

	vorlage = _vorlage(einst.firma, lieferant)
	if not vorlage.expense_account:
		raise frappe.ValidationError(_("Kein Aufwandskonto gefunden (Firma: Standard-Aufwandskonto fehlt)."))

	datum = getdate(teile["datum"])
	betrag = info.get("betrag")
	hinweise = [_("Automatisch erfasst aus: {0}").format(rel), _("Erkannt aus: {0}").format(info.get("quelle"))]
	if betrag is None:
		hinweise.append(_("BETRAG NICHT ERKANNT – bitte eintragen."))
	zahler = next((z for z in _zahler(einst) if z["kuerzel"] == teile["kuerzel"]), None)
	if zahler:
		hinweise.append(_("Privat bezahlt von {0} ({1}).").format(zahler["bezeichnung"], zahler["kuerzel"]))
	firmen_waehrung = frappe.get_cached_value("Company", einst.firma, "default_currency")
	waehrung = info.get("waehrung") or firmen_waehrung
	fremd_hinweis = ""
	if waehrung != firmen_waehrung:
		waehrung, betrag, fremd_hinweis = _fremdwaehrung(einst.firma, lieferant, waehrung, firmen_waehrung, betrag, datum)
		hinweise.append(fremd_hinweis)
	firmen_land = frappe.get_cached_value("Company", einst.firma, "country")
	lieferant_land = frappe.db.get_value("Supplier", lieferant, "country")
	if lieferant_land and firmen_land and lieferant_land != firmen_land:
		hinweise.append(_("Ausländischer Lieferant ({0}): Umsatzsteuer/Reverse Charge mit dem Steuerberater klären.").format(
			_(lieferant_land)))

	pi = frappe.new_doc("Purchase Invoice")
	pi.company = einst.firma
	pi.supplier = lieferant
	pi.bill_no = nummer
	pi.bill_date = datum
	pi.set_posting_time = 1
	pi.posting_date = datum
	pi.disable_rounded_total = cint(einst.rundung_aus)
	if waehrung != firmen_waehrung and frappe.db.exists("Currency", waehrung):
		pi.currency = waehrung
	pi.remarks = "\n".join(hinweise)
	zeile = {
		"item_name": vorlage.item_name or _("Rechnung {0} {1}").format(teile["lieferant"], nummer),
		"description": _("Rechnung {0} vom {1}").format(nummer, datum.strftime("%d.%m.%Y")),
		"qty": 1,
		"conversion_factor": 1,
		"rate": betrag or 0,
		"expense_account": vorlage.expense_account,
		"cost_center": vorlage.cost_center or frappe.get_cached_value("Company", einst.firma, "cost_center"),
	}
	if frappe.db.exists("UOM", "Nos"):
		zeile.update({"uom": "Nos", "stock_uom": "Nos"})
	pi.append("items", zeile)
	if einst.steuervorlage:
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		pi.taxes_and_charges = einst.steuervorlage
		pi.set("taxes", [])
		for zeile in get_taxes_and_charges("Purchase Taxes and Charges Template", einst.steuervorlage) or []:
			pi.append("taxes", zeile)
	pi.insert()
	_anhaengen(pi.name, pfad)

	name = _log_setzen(eintrag.name, pfad=rel, sha256=eintrag.sha256, groesse=pfad.stat().st_size,
		status="Entwurf angelegt", rechnung=pi.name, lieferant=lieferant,
		hinweis=_("Betrag nicht erkannt.") if betrag is None else fremd_hinweis)
	bericht["angelegt"].append({"datei": rel, "rechnung": pi.name, "betrag": betrag, "lieferant": lieferant,
		"waehrung": pi.currency, "hinweis": fremd_hinweis})
	return frappe._dict(name=name, sha256=eintrag.sha256, status="Entwurf angelegt", rechnung=pi.name, pfad=rel)


def _fremdwaehrung(firma, lieferant, waehrung, firmen_waehrung, betrag, datum):
	"""Rechnung in fremder Währung.

	ERPNext bucht eine Rechnung in Fremdwährung nur über ein Kreditorenkonto in
	dieser Währung. Gibt es das für den Lieferanten nicht, wird der Entwurf in
	Firmenwährung angelegt. Umgerechnet wird nur mit einem in ERPNext gespeicherten
	Kurs (Currency Exchange) – die App fragt keinen Kurs im Internet ab.
	Gibt (waehrung, betrag, hinweis) zurück.
	"""
	from erpnext.accounts.party import get_party_account_currency

	betrag_text = "{0} {1}".format(frappe.format_value(betrag, {"fieldtype": "Float", "precision": 2})
		if betrag is not None else "?", waehrung)
	try:
		konto_waehrung = get_party_account_currency("Supplier", lieferant, firma)
	except Exception:
		konto_waehrung = firmen_waehrung
	if konto_waehrung == waehrung and frappe.db.exists("Currency", waehrung):
		return waehrung, betrag, _("Rechnung in {0} – Umrechnungskurs prüfen (tatsächlich gezahlter Betrag?).").format(waehrung)

	kurs = frappe.get_all("Currency Exchange",
		filters={"from_currency": waehrung, "to_currency": firmen_waehrung, "date": ["<=", datum], "for_buying": 1},
		fields=["exchange_rate", "date"], order_by="date desc", limit=1)
	if betrag is not None and kurs and kurs[0].exchange_rate:
		umgerechnet = flt(betrag * flt(kurs[0].exchange_rate), 2)
		return firmen_waehrung, umgerechnet, _(
			"Rechnung über {0}. Umgerechnet mit gespeichertem Kurs {1} vom {2} – "
			"bitte mit dem tatsächlich gezahlten {3}-Betrag (Kontoauszug) vergleichen.").format(
			betrag_text, kurs[0].exchange_rate, getdate(kurs[0].date).strftime("%d.%m.%Y"), firmen_waehrung)
	return firmen_waehrung, betrag, _(
		"ACHTUNG: Rechnung über {0}. Der Betrag im Entwurf ist noch der {1}-Betrag – "
		"bitte den tatsächlich gezahlten {2}-Betrag aus dem Kontoauszug eintragen.").format(
		betrag_text, waehrung, firmen_waehrung)


# ---------------------------------------------------------------- PayPal-Zahlungsbelege

def _rechnung_zum_beleg(einst, ordner, beleg, lieferant=None, ausser_sha=None):
	"""Eingangsrechnung, zu der ein PayPal-Beleg gehört, oder None.

	1. Lieferanten-Rechnungsnummer = Rechnungsnummer/Transaktionscode im Beleg
	2. Rechnungsnummer des Belegs steht in der PDF der Rechnung
	   (z. B. als "Transaction ID" auf der Händlerrechnung)
	3. Lieferant und Betrag passen zu genau einer Rechnung
	"""
	if not lieferant and beleg.get("verkaeufer"):
		lieferant = _alias(beleg["verkaeufer"]) or _finde_lieferant(beleg["verkaeufer"], [])
	filters = {"company": einst.firma, "docstatus": ["<", 2]}
	if lieferant:
		filters["supplier"] = lieferant
	if beleg.get("datum"):
		d = getdate(beleg["datum"])
		filters["posting_date"] = ["between", [add_days(d, -31), add_days(d, 31)]]
	kandidaten = frappe.get_all("Purchase Invoice", filters=filters,
		fields=["name", "bill_no", "grand_total"], order_by="posting_date desc", limit=100)
	# Rechnungen, die (fälschlich) aus diesem Beleg selbst angelegt wurden, nie nehmen
	if ausser_sha:
		aus_beleg = set(frappe.get_all(LOG, filters={"sha256": ausser_sha, "rechnung": ["is", "set"]}, pluck="rechnung"))
		kandidaten = [k for k in kandidaten if k.name not in aus_beleg]

	nummern = [n for n in (beleg.get("rechnungsnummer"), beleg.get("transaktionscode")) if n]
	schluessel = {e.nummer_schluessel(n) for n in nummern}
	for k in kandidaten:
		if k.bill_no and e.nummer_schluessel(k.bill_no) in schluessel:
			return k.name
	for k in kandidaten:
		for pfad_rel in frappe.get_all(LOG, filters={"rechnung": k.name, "status": ["!=", "Zahlungsbeleg"]}, pluck="pfad"):
			pdf = ordner / pfad_rel
			if not pdf.is_file():
				continue
			try:
				text = e.pdf_lesen(pdf)[0]
			except Exception:
				continue
			if any(n in text for n in nummern):
				return k.name
	if lieferant:
		betraege = [b for b in (beleg.get("betrag"), beleg.get("betrag_eur")) if b]
		passend = [k for k in kandidaten if any(abs(flt(k.grand_total) - b) < 0.015 for b in betraege)]
		if len(passend) == 1:
			return passend[0].name
	return None


def _geld(betrag):
	return f"{flt(betrag):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _euro_betrag_uebernehmen(pi, beleg):
	"""Nur bei Entwürfen: tatsächlich gezahlten Euro-Betrag aus dem PayPal-Beleg eintragen."""
	if pi.docstatus != 0:
		return ""
	firmen_waehrung = frappe.get_cached_value("Company", pi.company, "default_currency")
	eur, fremd, waehrung = beleg.get("betrag_eur"), beleg.get("betrag"), beleg.get("waehrung")
	if firmen_waehrung != "EUR" or not eur or not fremd or not waehrung or waehrung == "EUR":
		return ""
	if pi.currency == waehrung:
		pi.conversion_rate = flt(eur / fremd, 9)
		hinweis = _("Kurs aus PayPal-Beleg übernommen: bezahlt {0} EUR = {1} {2}.").format(_geld(eur), _geld(fremd), waehrung)
	elif pi.currency == firmen_waehrung and len(pi.items) == 1 and abs(flt(pi.items[0].rate) - fremd) < 0.015:
		pi.items[0].rate = eur
		hinweis = _("Betrag aus PayPal-Beleg übernommen: {0} {1} = {2} EUR tatsächlich bezahlt.").format(_geld(fremd), waehrung, _geld(eur))
	else:
		return ""
	pi.remarks = ((pi.remarks or "") + "\n" + hinweis).strip()
	pi.save()
	return hinweis


def _beleg_verarbeiten(einst, ordner, pfad, rel, eintrag, beleg, kuerzel_liste, bericht):
	"""PayPal-Beleg der Rechnung zuordnen: umbenennen, anhängen, Euro-Betrag übernehmen.
	Als bezahlt markiert wird nichts – das bleibt beim Bankabgleich bzw. beim Zahler."""
	teile = e.zerlege_namen(pfad.name, kuerzel_liste) or e.zerlege_belegnamen(pfad.name, kuerzel_liste)
	lieferant = None
	if teile:
		lieferant = _alias(teile["lieferant"]) or _finde_lieferant(teile["lieferant"], [])
	rechnung = _rechnung_zum_beleg(einst, ordner, beleg, lieferant, eintrag.sha256)
	code = beleg.get("transaktionscode") or beleg.get("rechnungsnummer") or ""

	if not rechnung:
		hinweis = _("PayPal-Zahlungsbeleg {0}: noch keine passende Eingangsrechnung gefunden. "
			"Wird beim nächsten Lauf erneut gesucht.").format(code)
		_log_setzen(eintrag.name or frappe.db.get_value(LOG, {"sha256": eintrag.sha256}),
			pfad=rel, sha256=eintrag.sha256, groesse=pfad.stat().st_size, status="Zahlungsbeleg",
			rechnung=None, hinweis=hinweis)
		bericht["belege"].append({"datei": rel, "rechnung": None, "hinweis": hinweis})
		return

	pi = frappe.get_doc("Purchase Invoice", rechnung)
	# Einheitlich neben der Rechnung benennen: "… - Zahlungsbestätigung PayPal - Lieferant - Rechnungsnr[ - KÜRZEL]"
	datum = (teile or {}).get("datum") or beleg.get("datum") or str(pi.bill_date or pi.posting_date)
	lieferant_name = (teile or {}).get("lieferant") or beleg.get("verkaeufer") or pi.supplier_name or pi.supplier
	neu = e.belegname(datum, lieferant_name, pi.bill_no or pi.name, (teile or {}).get("kuerzel", ""))
	alter_rel = rel
	if pfad.name != neu:
		try:
			ziel = e.freier_name(pfad.parent, neu)
			pfad.rename(ziel)
			pfad, rel = ziel, _rel(ordner, ziel)
		except OSError:
			pass

	hinweise = []
	if not frappe.db.exists("File", {"attached_to_doctype": "Purchase Invoice", "attached_to_name": rechnung,
			"file_name": pfad.name}):
		_anhaengen(rechnung, pfad)
		hinweise.append(_("Beleg angehängt."))
	try:
		h = _euro_betrag_uebernehmen(pi, beleg)
		if h:
			hinweise.append(h)
	except Exception as fehler:
		frappe.clear_last_message()
		hinweise.append(_("Euro-Betrag nicht übernommen: {0}").format(fehler))
	text = _("PayPal-Zahlungsbeleg {0} zugeordnet ({1}).").format(code, pfad.name)
	if beleg.get("betrag_eur") and beleg.get("waehrung") and beleg["waehrung"] != "EUR":
		text += " " + _("Bezahlt: {0} EUR = {1} {2}.").format(_geld(beleg["betrag_eur"]), _geld(beleg.get("betrag")), beleg["waehrung"])
	pi.add_comment("Comment", text=" ".join([text] + hinweise))

	hinweis = " ".join([_("Zahlungsbeleg zu {0}.").format(rechnung)] + hinweise)
	_log_setzen(eintrag.name or frappe.db.get_value(LOG, {"sha256": eintrag.sha256}),
		pfad=rel, sha256=eintrag.sha256, groesse=pfad.stat().st_size, status="Zahlungsbeleg",
		rechnung=rechnung, hinweis=hinweis[:500])
	bericht["belege"].append({"datei": alter_rel, "neu": rel if rel != alter_rel else "", "rechnung": rechnung,
		"hinweis": " ".join(hinweise)})


def rechnung_geloescht(doc, method=None):
	"""Beim Löschen einer Eingangsrechnung die Verknüpfung in 'Eingangsrechnung Datei' lösen,
	damit das Löschen nicht blockiert. Die Datei wird beim nächsten Lauf neu geprüft."""
	for log in frappe.get_all(LOG, filters={"rechnung": doc.name}, fields=["name", "status"]):
		werte = {"rechnung": None}
		if log.status != "Zahlungsbeleg":
			werte.update({"status": "Fehler", "hinweis": _(
				"Eingangsrechnung {0} wurde gelöscht. Die Datei wird beim nächsten Lauf neu erfasst – "
				"ist sie keine Rechnung, bitte umbenennen (z. B. 'Zahlungsbestätigung').").format(doc.name)})
		frappe.db.set_value(LOG, log.name, werte)


# ---------------------------------------------------------------- Neue Lieferanten

def _alias(name_im_dateinamen):
	"""Lieferant, der früher schon einer Datei mit diesem Namen zugeordnet wurde."""
	if not name_im_dateinamen:
		return None
	muster = f"% - Rechnung - {name_im_dateinamen} - %"
	treffer = frappe.get_all(LOG, filters={"pfad": ["like", muster], "lieferant": ["is", "set"]},
		pluck="lieferant", limit=1)
	return treffer[0] if treffer and frappe.db.exists("Supplier", treffer[0]) else None


def _naechste_kreditornummer(einst):
	if not cint(einst.kreditor_ab) or not frappe.get_meta("Party Account").has_field("debtor_creditor_number"):
		return ""
	nummern = frappe.get_all("Party Account",
		filters={"parenttype": "Supplier", "company": einst.firma, "debtor_creditor_number": ["is", "set"]},
		pluck="debtor_creditor_number")
	zahlen = [int(n) for n in nummern if str(n).strip().isdigit()]
	return str(max([cint(einst.kreditor_ab) - 1] + zahlen) + 1)


def _zahlungsbedingung(einst, tage):
	vorlagen = frappe.get_all("Payment Terms Template", pluck="name")
	if tage == 0:
		sofort = [v for v in vorlagen if re.search(r"sofort|immediate|vorkasse", v, re.IGNORECASE)]
		if sofort:
			return sofort[0]
	elif tage:
		mit_tagen = []
		for v in vorlagen:
			m = re.match(r"\s*(\d+)\s*(?:Tage|days)\s*(?:netto|net)?\s*$", v, re.IGNORECASE)
			if m:
				mit_tagen.append((abs(int(m.group(1)) - tage), v))
		if mit_tagen:
			return min(mit_tagen)[1]
	return einst.zahlungsbedingung or ""


def _steuerkategorie(einst, land, tax_id):
	firmen_land = frappe.get_cached_value("Company", einst.firma, "country")
	firmen_prefix = {"Germany": "DE", "Austria": "AT"}.get(firmen_land, "")
	if (firmen_prefix and (tax_id or "").upper().startswith(firmen_prefix)) or (land and land == firmen_land):
		return einst.steuerkat_inland or ""
	if land and lf.ist_eu(land):
		return einst.steuerkat_eu or ""
	if land:
		return einst.steuerkat_drittland or ""
	return ""


def _vorschlag(einst, pfad, teile, info, eigene):
	"""Daten für einen neuen Lieferanten aus der Rechnung (zum Prüfen im Dialog)."""
	try:
		text, xml = e.pdf_lesen(pfad)
	except Exception:
		text, xml = "", None
	try:
		datum = getdate(teile["datum"])
	except Exception:
		datum = None
	# Eigene Daten (Benutzer, Firma) nie als Lieferantendaten übernehmen
	eigene = list(eigene)
	for nutzer in frappe.get_all("User", filters={"user_type": "System User", "enabled": 1},
			fields=["name", "email", "first_name", "last_name"]):
		for wert in (nutzer.name, nutzer.email):
			if wert and "@" in wert:
				eigene.append(wert.lower())
		if nutzer.first_name and nutzer.last_name:
			eigene.append(f"{nutzer.first_name}.{nutzer.last_name}".lower())
	for feld in ("email", "website"):
		wert = frappe.get_cached_value("Company", einst.firma, feld) if frappe.get_meta("Company").has_field(feld) else None
		if wert:
			eigene.append(wert.lower().replace("https://", "").replace("http://", "").replace("www.", ""))
	v = lf.lieferant_vorschlag(text, xml, teile["lieferant"], eigene, datum)
	if not v.get("name"):
		v["name"] = teile["lieferant"]
	if v.get("land") and not frappe.db.exists("Country", v["land"]):
		v["land"] = ""
	firmen_waehrung = frappe.get_cached_value("Company", einst.firma, "default_currency")
	v["waehrung"] = info.get("waehrung") or firmen_waehrung
	v["standardwaehrung"] = firmen_waehrung if v["waehrung"] == firmen_waehrung else ""
	v["zahlungsbedingung"] = _zahlungsbedingung(einst, v.get("zahlungsziel_tage"))
	v["steuerkategorie"] = _steuerkategorie(einst, v.get("land"), v.get("tax_id"))
	v["lieferantengruppe"] = einst.lieferantengruppe or ""
	v["kreditor"] = _naechste_kreditornummer(einst)
	v["kreditor_moeglich"] = 1 if frappe.get_meta("Party Account").has_field("debtor_creditor_number") else 0
	notiz = []
	if v.get("steuernummer"):
		notiz.append(_("Steuernummer: {0}").format(v["steuernummer"]))
	if v.get("register"):
		notiz.append(_("Handelsregister: {0}").format(v["register"]))
	if v.get("kontoinhaber"):
		notiz.append(_("Kontoinhaber laut Rechnung: {0}").format(v["kontoinhaber"]))
	if v["waehrung"] != firmen_waehrung:
		notiz.append(_("Rechnungen in {0}.").format(v["waehrung"]))
	notiz.append(_("Angelegt aus Rechnung {0} vom {1} ({2}).").format(
		teile["nummer"], datum.strftime("%d.%m.%Y") if datum else teile["datum"], v.get("quelle")))
	v["notiz"] = "\n".join(notiz)
	v["pfad"] = _rel(Path(einst.ordner), pfad)
	return v


@frappe.whitelist(methods=["POST"])
def lieferant_anlegen(daten, dateien=None):
	"""Legt Lieferant + Adresse + Kontakt + Bankkonto an (Daten aus dem Dialog)."""
	if not frappe.has_permission("Supplier", "create"):
		frappe.throw(_("Keine Berechtigung zum Anlegen von Lieferanten."), frappe.PermissionError)
	einst = _einstellungen()
	d = frappe._dict(json.loads(daten) if isinstance(daten, str) else daten)
	dateien = json.loads(dateien) if isinstance(dateien, str) else (dateien or [])
	name = (d.supplier_name or "").strip()
	if not name:
		frappe.throw(_("Bitte den Namen des Lieferanten eintragen."))
	if frappe.db.exists("Supplier", {"supplier_name": name}):
		frappe.throw(_("Einen Lieferanten „{0}“ gibt es schon.").format(name))

	firma = einst.firma
	sup = frappe.new_doc("Supplier")
	sup.supplier_name = name
	sup.supplier_type = "Company"
	sup.supplier_group = d.supplier_group or None
	sup.country = d.land or None
	sup.tax_id = (d.tax_id or "").strip() or None
	sup.tax_category = d.tax_category or None
	sup.payment_terms = d.payment_terms or None
	sup.website = (d.website or "").strip() or None
	sup.supplier_details = (d.notiz or "").strip() or None
	konto = frappe.get_cached_value("Company", firma, "default_payable_account")
	waehrung_hinweis = None
	if d.default_currency:
		# ERPNext erlaubt als Standardwährung nur die Firmenwährung oder die Währung
		# des Kreditorenkontos. Sonst leer lassen – Rechnungen gehen trotzdem in Fremdwährung.
		erlaubt = {frappe.get_cached_value("Company", firma, "default_currency")}
		if konto:
			erlaubt.add(frappe.get_cached_value("Account", konto, "account_currency"))
		if d.default_currency in erlaubt:
			sup.default_currency = d.default_currency
		else:
			waehrung_hinweis = _("Standardwährung {0} nicht gesetzt: Das Kreditorenkonto wird in {1} geführt. Rechnungen in {0} gehen trotzdem.").format(
				d.default_currency, ", ".join(sorted(w for w in erlaubt if w)))
	if konto:
		zeile = {"company": firma, "account": konto}
		if d.kreditor and frappe.get_meta("Party Account").has_field("debtor_creditor_number"):
			zeile["debtor_creditor_number"] = str(d.kreditor).strip()
		sup.append("accounts", zeile)
	sup.insert()

	ergebnis = {"lieferant": sup.name, "adresse": None, "kontakt": None, "bankkonto": None, "hinweise": []}
	if waehrung_hinweis:
		ergebnis["hinweise"].append(waehrung_hinweis)

	if d.strasse or d.ort:
		land = d.land or frappe.get_cached_value("Company", firma, "country")
		adresse = frappe.get_doc({
			"doctype": "Address",
			"address_title": name,
			"address_type": "Billing",
			"address_line1": d.strasse or d.ort,
			"address_line2": d.adresszusatz or None,
			"pincode": d.plz or None,
			"city": d.ort or "-",
			"state": d.bundesland or None,
			"country": land,
			"email_id": d.email or None,
			"phone": d.telefon or None,
			"is_primary_address": 1,
			"links": [{"link_doctype": "Supplier", "link_name": sup.name}],
		})
		adresse.insert()
		ergebnis["adresse"] = adresse.name
		from frappe.contacts.doctype.address.address import get_address_display

		frappe.db.set_value("Supplier", sup.name, {
			"supplier_primary_address": adresse.name,
			"primary_address": get_address_display(adresse.as_dict()),
		})

	if d.email or d.telefon:
		kontakt = frappe.get_doc({
			"doctype": "Contact",
			"first_name": name[:140],
			"is_primary_contact": 1,
			"links": [{"link_doctype": "Supplier", "link_name": sup.name}],
		})
		if d.email:
			kontakt.append("email_ids", {"email_id": d.email.strip(), "is_primary": 1})
		if d.telefon:
			kontakt.append("phone_nos", {"phone": d.telefon.strip(), "is_primary_phone": 1})
		kontakt.insert()
		ergebnis["kontakt"] = kontakt.name
		frappe.db.set_value("Supplier", sup.name, "supplier_primary_contact", kontakt.name)

	iban = re.sub(r"\s", "", (d.iban or "").upper())
	if iban:
		bank = (d.bank or "").strip()
		if not bank:
			ergebnis["hinweise"].append(_("IBAN ohne Banknamen – Bankkonto nicht angelegt, IBAN steht in den Details."))
			frappe.db.set_value("Supplier", sup.name, "supplier_details",
				((sup.supplier_details or "") + "\n" + _("IBAN: {0} {1}").format(iban, d.bic or "")).strip())
		else:
			if not frappe.db.exists("Bank", bank):
				frappe.get_doc({"doctype": "Bank", "bank_name": bank, "swift_number": d.bic or None}).insert()
			konto_bank = frappe.get_doc({
				"doctype": "Bank Account",
				"account_name": (d.kontoinhaber or name)[:140],
				"bank": bank,
				"iban": iban,
				"party_type": "Supplier",
				"party": sup.name,
				"is_default": 1,
			})
			konto_bank.insert()
			ergebnis["bankkonto"] = konto_bank.name

	# Dateien diesem Lieferanten zuordnen, damit der nächste Lauf ihn sicher findet
	for rel in dateien:
		name_log = frappe.db.get_value(LOG, {"pfad": rel})
		if name_log:
			frappe.db.set_value(LOG, name_log, {"lieferant": sup.name,
				"hinweis": _("Lieferant {0} angelegt – wird beim nächsten Lauf erfasst.").format(sup.name)})
	frappe.db.commit()
	return ergebnis


# ---------------------------------------------------------------- Bezahlte verschieben

def _aktuelle_rechnung(name):
	"""Bei stornierten und berichtigten Rechnungen der neuesten Fassung folgen."""
	for _i in range(10):
		werte = frappe.db.get_value("Purchase Invoice", name, ["name", "docstatus"], as_dict=True)
		if not werte:
			return None
		if werte.docstatus != 2:
			return werte.name
		neu = frappe.db.get_value("Purchase Invoice", {"amended_from": name}, "name")
		if not neu:
			return None
		name = neu
	return None


def _verschieben_wenn_bezahlt(einst, ordner, pfad, rel, eintrag, zahler, kuerzel_liste, bericht):
	offen = (einst.ordner_offen or "").strip("/")
	if not offen or not (rel + "/").startswith(offen + "/") or rel == offen:
		return
	rechnung = _aktuelle_rechnung(eintrag.rechnung)
	if not rechnung:
		return
	pi = frappe.db.get_value("Purchase Invoice", rechnung, ["docstatus", "status", "bill_date", "posting_date"], as_dict=True)
	if pi.docstatus != 1 or pi.status != "Paid":
		return

	# Privat bezahlt? -> Buchungssatz auf das Konto eines Zahlers
	gefunden = None
	buchungen = frappe.get_all("Journal Entry Account",
		filters={"reference_type": "Purchase Invoice", "reference_name": rechnung, "docstatus": 1}, pluck="parent")
	if buchungen:
		konten = set(frappe.get_all("Journal Entry Account",
			filters={"parent": ["in", buchungen], "docstatus": 1}, pluck="account"))
		gefunden = next((z for z in zahler if z["konto"] and z["konto"] in konten), None)

	datum = getdate(pi.bill_date or pi.posting_date)
	platzhalter = {
		"jahr": f"{datum.year}",
		"monat": f"{datum.month:02d}",
		"monatsname": e.MONATSNAMEN[datum.month],
		"kuerzel": gefunden["kuerzel"] if gefunden else "",
	}
	muster = einst.ziel_zahler if gefunden else einst.ziel_bank
	if not muster:
		return
	ziel_ordner_rel = muster.format(**platzhalter).strip("/")
	if ".." in ziel_ordner_rel.split("/"):
		return
	ziel_ordner = ordner / ziel_ordner_rel
	ziel_ordner.mkdir(parents=True, exist_ok=True)

	neuer_name = pfad.name
	if gefunden:
		teile = e.zerlege_namen(pfad.name, kuerzel_liste)
		if teile and not teile["kuerzel"]:
			neuer_name = e.zielname(teile["datum"], teile["lieferant"], teile["nummer"], gefunden["kuerzel"])
	ziel = e.freier_name(ziel_ordner, neuer_name)
	shutil.move(str(pfad), str(ziel))
	neu_rel = _rel(ordner, ziel)
	frappe.db.set_value(LOG, eintrag.name, {"pfad": neu_rel, "status": "Verschoben", "rechnung": rechnung,
		"hinweis": _("Bezahlt – verschoben aus {0}").format(rel)})
	bericht["verschoben"].append({"datei": rel, "nach": neu_rel, "rechnung": rechnung})

	# Zugeordnete Zahlungsbelege aus dem Ordner für offene Rechnungen mitnehmen
	for beleg in frappe.get_all(LOG, filters={"rechnung": ["in", list({eintrag.rechnung, rechnung})],
			"status": "Zahlungsbeleg"}, fields=["name", "pfad"]):
		quelle = ordner / beleg.pfad
		if not quelle.is_file() or not (beleg.pfad + "/").startswith(offen + "/"):
			continue
		ziel_beleg = e.freier_name(ziel_ordner, quelle.name)
		shutil.move(str(quelle), str(ziel_beleg))
		frappe.db.set_value(LOG, beleg.name, "pfad", _rel(ordner, ziel_beleg))
		bericht["verschoben"].append({"datei": beleg.pfad, "nach": _rel(ordner, ziel_beleg), "rechnung": rechnung})


# ---------------------------------------------------------------- PDF ansehen

@frappe.whitelist()
def datei_anzeigen(pfad):
	"""Zeigt eine PDF aus dem Rechnungsordner im Browser (zum Prüfen vor dem Umbenennen)."""
	_darf()
	einst = _einstellungen()
	datei = _sicherer_pfad(_ordner(einst), pfad)
	frappe.local.response.filename = datei.name
	frappe.local.response.filecontent = datei.read_bytes()
	frappe.local.response.type = "pdf"
