"""
Erkennung von Rechnungsdaten aus PDFs – ohne Frappe, ohne Internet.

Die Regeln stammen aus dem Tool "Rechnungen umbenennen" und
wurden um Betrag, USt-IdNr. und ZUGFeRD/Factur-X erweitert. Alles läuft lokal
auf dem Server; keine Daten verlassen das System.
"""

import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from pypdf import PdfReader

# Schema der fertigen Dateinamen
#   JJJJ-MM-TT - Rechnung - Lieferant - Rechnungsnummer[ - KÜRZEL].pdf
ZIELMUSTER = re.compile(r"^\d{4}-\d{2}-\d{2} - Rechnung - .+ - .+\.pdf$", re.IGNORECASE)

# Dateien, die sicher keine Rechnungen sind (werden nie angeboten)
KEINE_RECHNUNG = re.compile(
	r"berweisungsbest|zahlungsbest|zahlungsavis|kontoauszug|mahnung|lieferschein|auftragsbest|angebot",
	re.IGNORECASE,
)

MONATE = {
	"jan": 1, "januar": 1, "january": 1, "jänner": 1,
	"feb": 2, "februar": 2, "february": 2,
	"mär": 3, "mar": 3, "märz": 3, "maerz": 3, "march": 3,
	"apr": 4, "april": 4,
	"mai": 5, "may": 5,
	"jun": 6, "juni": 6, "june": 6,
	"jul": 7, "juli": 7, "july": 7,
	"aug": 8, "august": 8,
	"sep": 9, "sept": 9, "september": 9,
	"okt": 10, "oct": 10, "oktober": 10, "october": 10,
	"nov": 11, "november": 11,
	"dez": 12, "dec": 12, "dezember": 12, "december": 12,
}

MONATSNAMEN = [
	"", "Januar", "Februar", "März", "April", "Mai", "Juni",
	"Juli", "August", "September", "Oktober", "November", "Dezember",
]

RE_DATUM = re.compile(
	r"(?P<d1>\d{1,2})\.\s?(?P<m1>\d{1,2})\.\s?(?P<y1>\d{4}|\d{2})(?!\d)"
	r"|(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})"
	r"|(?P<d3>\d{1,2})\.?\s+(?P<m3>[A-Za-zäÄ]{3,9})\.?\s+(?P<y3>\d{4})"
	r"|(?P<m4>[A-Za-z]{3,9})\.?\s+(?P<d4>\d{1,2}),?\s+(?P<y4>\d{4})"
)

DATUM_LABELS = [
	r"Rechnungsdatum", r"Rechnungs-Datum", r"Belegdatum", r"Invoice\s*date",
	r"Datum der Rechnung", r"Ausstellungsdatum", r"Date of issue", r"Datum",
]

NUMMER_LABELS = [
	r"Rechnungsnummer", r"Rechnungs-?\s?Nr\.?", r"Rechnung\s*Nr\.?", r"Rechnung\s*#",
	r"Re\.?-?\s?Nr\.?", r"Belegnummer", r"Beleg-?\s?Nr\.?",
	r"Invoice\s*(?:number|no\.?|#)", r"Rechnung",
]

# Bruttobetrag: Reihenfolge = Vorrang
BETRAG_LABELS = [
	r"Zahlbetrag", r"Amount\s+Paid", r"Rechnungsbetrag(?:\s+von)?", r"Zu\s+zahlender\s+Betrag", r"Endbetrag",
	r"Gesamtbetrag(?:\s*\(?brutto\)?)?", r"Summe\s*\(?brutto\)?", r"Bruttobetrag",
	r"Gesamtpreis", r"Amount\s+due", r"Total\s+due", r"Gesamtsumme", r"Gesamt", r"Total\s+Amount", r"Total",
]
# Zeilen, die zwar ein Label enthalten, aber nicht den Bruttobetrag
BETRAG_FALSCH = re.compile(
	r"Gesamtbetrag\s+(?:MwSt|USt)|(?:MwSt|USt)\.?\s+Gesamt|netto|exkl|Zwischensumme|Stammkapital",
	re.IGNORECASE,
)
RE_BETRAG = re.compile(r"(?<![\d,.])(\d{1,3}(?:\.\d{3})+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+,\d{2}|\d+\.\d{2})(?![\d])")

RECHTSFORMEN = (r"(?:GmbH\s*&\s*Co\.?\s*KG|GmbH|AG|KG|OHG|UG|e\.\s?K\.|GbR|SE|Ltd\.?|Limited|Inc\.?|LLC|"
	r"Corp\.?|Corporation|B\.\s?V\.?|S\.?A\.?R\.?L\.?|PartG(?:\s+mbB)?)")
RE_FIRMA = re.compile(rf"([A-ZÄÖÜ0-9][\w&.,'\- ]{{1,60}}?\s{RECHTSFORMEN})(?![\w])")
RE_DOMAIN = re.compile(r"(?:www\.|@)([a-z0-9\-]+)\.(?:de|com|net|org|eu|at|ch|io|shop|ai|co|app)\b", re.IGNORECASE)
GENERISCHE_DOMAINS = {"gmail", "gmx", "web", "t-online", "outlook", "hotmail", "yahoo", "icloud", "posteo", "paypal"}
RE_UST_ID = re.compile(r"\b(DE\s?\d{3}\s?\d{3}\s?\d{3}|ATU\d{8}|LU\d{8}|NL\d{9}B\d{2}|IE\d{7}[A-Z]{1,2})\b")


# ---------------------------------------------------------------- Einstellungsdateien

def lies_liste(datei):
	if not datei or not Path(datei).exists():
		return []
	zeilen = []
	for zeile in Path(datei).read_text(encoding="utf-8").splitlines():
		zeile = zeile.strip()
		if zeile and not zeile.startswith("#"):
			zeilen.append(zeile)
	return zeilen


def lade_lieferanten(datei=None, zeilen=None):
	"""'Suchbegriff = Name'; '!EigeneFirma' wird nie als Lieferant genommen.
	Quelle: Textzeilen (aus den Einstellungen) oder eine lieferanten.txt."""
	bekannte, eigene = [], []
	if zeilen is None:
		zeilen = lies_liste(datei)
	else:
		zeilen = [z.strip() for z in zeilen if z.strip() and not z.strip().startswith("#")]
	for zeile in zeilen:
		if zeile.startswith("!"):
			eigene.append(zeile[1:].strip().lower())
		elif "=" in zeile:
			such, name = (t.strip() for t in zeile.split("=", 1))
			bekannte.append((such.lower(), name))
		else:
			bekannte.append((zeile.lower(), zeile))
	return bekannte, eigene


def lade_gesellschafter(datei):
	"""'Name = Kürzel' -> Liste von {name, kuerzel}."""
	ergebnis = []
	for zeile in lies_liste(datei):
		if "=" in zeile:
			name, kuerzel = (t.strip() for t in zeile.split("=", 1))
			kuerzel = saeubern(kuerzel)
			if name and kuerzel:
				ergebnis.append({"name": name, "kuerzel": kuerzel})
	return ergebnis


# ---------------------------------------------------------------- PDF lesen

def pdf_lesen(pfad, max_seiten=3):
	"""Liefert (text, factur_x_xml_bytes_oder_None)."""
	reader = PdfReader(str(pfad))
	teile = [(seite.extract_text() or "") for seite in reader.pages[:max_seiten]]
	xml = None
	try:
		for name, inhalte in (reader.attachments or {}).items():
			if name.lower().endswith(".xml") and inhalte:
				xml = inhalte[0]
				break
	except Exception:
		xml = None
	return "\n".join(teile), xml


def lies_factur_x(xml_bytes):
	"""Liest die wichtigsten Felder aus ZUGFeRD/Factur-X (CII) oder XRechnung (UBL)."""
	try:
		wurzel = ET.fromstring(xml_bytes)
	except ET.ParseError:
		return {}

	def lokal(tag):
		return tag.rsplit("}", 1)[-1]

	def finde(knoten, *pfad):
		aktuell = [knoten]
		for teil in pfad:
			naechste = []
			for k in aktuell:
				naechste.extend(c for c in k if lokal(c.tag) == teil)
			aktuell = naechste
			if not aktuell:
				return None
		return aktuell[0]

	info = {}
	if lokal(wurzel.tag) == "CrossIndustryInvoice":
		doc = finde(wurzel, "ExchangedDocument")
		if doc is not None:
			nr = finde(doc, "ID")
			info["nummer"] = nr.text.strip() if nr is not None and nr.text else ""
			dt = finde(doc, "IssueDateTime", "DateTimeString")
			if dt is not None and dt.text and len(dt.text.strip()) >= 8:
				t = dt.text.strip()
				info["datum"] = f"{t[0:4]}-{t[4:6]}-{t[6:8]}"
		handel = finde(wurzel, "SupplyChainTradeTransaction")
		if handel is not None:
			verkaeufer = finde(handel, "ApplicableHeaderTradeAgreement", "SellerTradeParty")
			if verkaeufer is not None:
				name = finde(verkaeufer, "Name")
				info["lieferant"] = name.text.strip() if name is not None and name.text else ""
				for reg in verkaeufer.iter():
					if lokal(reg.tag) == "ID" and reg.get("schemeID") == "VA" and reg.text:
						info["ust_id"] = reg.text.strip().replace(" ", "")
			waehrung = finde(handel, "ApplicableHeaderTradeSettlement", "InvoiceCurrencyCode")
			if waehrung is not None and waehrung.text:
				info["waehrung"] = waehrung.text.strip()
			summen = finde(handel, "ApplicableHeaderTradeSettlement",
				"SpecifiedTradeSettlementHeaderMonetarySummation")
			if summen is not None:
				for feld in ("GrandTotalAmount", "DuePayableAmount"):
					b = finde(summen, feld)
					if b is not None and b.text:
						info["betrag"] = float(b.text.strip())
						break
	elif lokal(wurzel.tag) == "Invoice":  # UBL / XRechnung
		for kind in wurzel:
			if lokal(kind.tag) == "ID" and kind.text:
				info["nummer"] = kind.text.strip()
			elif lokal(kind.tag) == "IssueDate" and kind.text:
				info["datum"] = kind.text.strip()
		partei = finde(wurzel, "AccountingSupplierParty", "Party")
		if partei is not None:
			for k in partei.iter():
				if lokal(k.tag) == "RegistrationName" and k.text:
					info["lieferant"] = k.text.strip()
				if lokal(k.tag) == "CompanyID" and k.text and RE_UST_ID.match(k.text.strip()):
					info["ust_id"] = k.text.strip()
		summe = finde(wurzel, "LegalMonetaryTotal", "PayableAmount")
		if summe is not None and summe.text:
			info["betrag"] = float(summe.text.strip())
	return info


# ---------------------------------------------------------------- Einzelne Felder

def baue_datum(m):
	try:
		if m.group("d1"):
			j = int(m.group("y1"))
			j = j + 2000 if j < 100 else j
			return date(j, int(m.group("m1")), int(m.group("d1")))
		if m.group("y2"):
			return date(int(m.group("y2")), int(m.group("m2")), int(m.group("d2")))
		if m.group("d3"):
			monat = MONATE.get(m.group("m3").lower().rstrip("."))
			return date(int(m.group("y3")), monat, int(m.group("d3"))) if monat else None
		if m.group("m4"):
			monat = MONATE.get(m.group("m4").lower().rstrip("."))
			return date(int(m.group("y4")), monat, int(m.group("d4"))) if monat else None
	except ValueError:
		return None
	return None


def finde_datum(text):
	for label in DATUM_LABELS:
		for treffer in re.finditer(label, text, re.IGNORECASE):
			m = RE_DATUM.search(text[treffer.end(): treffer.end() + 60])
			if m:
				d = baue_datum(m)
				if d:
					return d
	for m in RE_DATUM.finditer(text):
		d = baue_datum(m)
		if d and 2000 <= d.year <= date.today().year + 1:
			return d
	return None


def finde_nummer(text):
	for label in NUMMER_LABELS:
		muster = re.compile(
			rf"{label}\s*[:#.]?\s*(?:\n\s*)?([A-Z0-9][A-Z0-9\-/_.]{{2,30}}(?: \d+(?![.\d]))*)",
			re.IGNORECASE,
		)
		for m in muster.finditer(text):
			wert = m.group(1).rstrip(".-/")
			if re.search(r"\d", wert) and not RE_DATUM.fullmatch(wert):
				return wert
	return None


def finde_lieferant(text, bekannte, eigene):
	klein = text.lower()
	for such, name in bekannte:
		if such in klein:
			return name
	for m in RE_FIRMA.finditer(text):
		firma = " ".join(m.group(1).split())
		firma = re.sub(r"^(?:Rechnung|Invoice|Von|From|Absender)[:\s]+", "", firma, flags=re.IGNORECASE)
		if not any(e in firma.lower() for e in eigene):
			return firma
	for m in RE_DOMAIN.finditer(text):
		name = m.group(1)
		if name.lower() not in GENERISCHE_DOMAINS and not any(e in name.lower() for e in eigene):
			return name.capitalize()
	return None


def zahl(wert):
	wert = wert.strip()
	if re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{2}", wert):  # 1,234.56 (englisch)
		return float(wert.replace(",", ""))
	if "," in wert:
		wert = wert.replace(".", "").replace(",", ".")
	return float(wert)


def finde_betrag(text):
	"""Bruttobetrag an einem bekannten Label: zuerst dahinter (gleiche oder nächste
	Zeile), sonst direkt davor (manche PDFs geben Werte vor den Beschriftungen aus)."""
	for label in BETRAG_LABELS:
		for treffer in re.finditer(rf"\b{label}\b", text, re.IGNORECASE):
			zeile_start = text.rfind("\n", 0, treffer.start()) + 1
			zeile_ende = text.find("\n", treffer.end())
			zeile = text[zeile_start: zeile_ende if zeile_ende >= 0 else len(text)]
			if BETRAG_FALSCH.search(zeile):
				continue
			danach = text[treffer.end(): treffer.end() + 80]
			# nur bis zum nächsten Label schauen
			naechstes = re.search(r"\n[^\d\n$€£]*[A-Za-zÄÖÜäöü]{3,}[^\n]*:\s*\n", danach)
			if naechstes:
				danach = danach[: naechstes.start()]
			m = RE_BETRAG.search(danach)
			if not m:
				# Werte davor nur, wenn die Beschriftung allein in ihrer Zeile steht
				if not re.fullmatch(rf"\s*{label}\s*:?\s*", zeile, re.IGNORECASE):
					continue
				davor = text[max(zeile_start - 60, 0): zeile_start]
				treffer_davor = RE_BETRAG.findall(davor)
				if treffer_davor:
					try:
						return zahl(treffer_davor[-1])
					except ValueError:
						continue
				continue
			try:
				return zahl(m.group(1))
			except ValueError:
				continue
	return None


def finde_waehrung(text):
	"""ISO-Code der Rechnungswährung (EUR, USD, GBP, CHF) aus Symbolen und Codes."""
	zaehler = {
		"EUR": len(re.findall(r"€|\bEUR\b", text)),
		"USD": len(re.findall(r"\$|\bUSD\b|\bUS-Dollar", text)),
		"GBP": len(re.findall(r"£|\bGBP\b", text)),
		"CHF": len(re.findall(r"\bCHF\b|\bSFr\b", text)),
	}
	zaehler["EUR"] -= len(re.findall(r"Stammkapital:?\s*[\d.,]+\s*(?:€|EUR)", text))
	beste = max(zaehler, key=zaehler.get)
	return beste if zaehler[beste] > 0 else "EUR"


def finde_ust_ids(text):
	return [u.replace(" ", "") for u in RE_UST_ID.findall(text)]


# ---------------------------------------------------------------- PayPal-Zahlungsbelege

# Dateiname eines Zahlungsbelegs (liegt neben seiner Rechnung):
#   JJJJ-MM-TT - Zahlungsbestätigung PayPal - Lieferant - Rechnungsnummer[ - KÜRZEL].pdf
BELEGMUSTER = re.compile(r"^\d{4}-\d{2}-\d{2} - Zahlungsbestätigung PayPal - .+ - .+\.pdf$", re.IGNORECASE)

# Ein PayPal-Beleg hat einen Transaktionscode UND typische Texte des Belegs.
# Rechnungen von Händlern, die nur "Payment Type: PayPal" oder "Transaction ID"
# enthalten, haben diese Texte nicht und bleiben Rechnungen.
PAYPAL_CODE = re.compile(r"Transaktionscode|Transaction\s+(?:ID|code)", re.IGNORECASE)
PAYPAL_BELEG = re.compile(
	r"Käuferschutz|Bestellübersicht|Zahlung\s+gesendet|Buyer\s+Protection|Purchase\s+Protection|You\s+sent\s+a\s+payment",
	re.IGNORECASE,
)
RE_ZAHL = r"\d[\d.,]*\d|\d"
SYMBOLE = {"€": "EUR", "$": "USD", "£": "GBP"}


def ist_belegname(dateiname):
	return bool(BELEGMUSTER.match(unicodedata.normalize("NFC", dateiname or "")))


def paypal_beleg(text):
	"""Erkennt einen PayPal-Zahlungsbeleg und liest ihn aus, sonst None.

	Rückgabe: transaktionscode, rechnungsnummer (die der Händler an PayPal gab,
	oft die Transaction ID auf seiner Rechnung), verkaeufer, datum, betrag +
	waehrung (wie beim Händler bezahlt), betrag_eur und kurs (falls umgerechnet).
	"""
	if not text or "paypal" not in text.lower():
		return None
	if not (PAYPAL_CODE.search(text) and PAYPAL_BELEG.search(text)):
		return None
	b = {"transaktionscode": "", "rechnungsnummer": "", "verkaeufer": "", "datum": "",
		"betrag": None, "waehrung": "", "betrag_eur": None, "kurs": None}

	m = re.search(r"(?:Transaktionscode|Transaction\s+(?:ID|code))\s*:?\s*\n?\s*([A-Z0-9]{17})\b", text, re.IGNORECASE)
	if m:
		b["transaktionscode"] = m.group(1).upper()
	m = re.search(r"(?:Rechnungsnummer|Rechnungs-?\s?Nr\.?|Invoice\s+(?:ID|number|no\.?))\s*:?\s*\n?\s*"
		r"([A-Z0-9][A-Z0-9\-/_.]{2,40})", text, re.IGNORECASE)
	if m:
		b["rechnungsnummer"] = m.group(1).rstrip(".-/")
	m = re.search(r"(?:Verkäufer|Seller(?:\s+info)?)\s*:?\s*\n\s*([^\n]+)", text, re.IGNORECASE)
	if not m:
		m = re.match(r"\s*([^\n]+?)\s+[−\-–]\s?\d", text)  # erste Zeile "Firma −12,34 $ USD"
	if m:
		b["verkaeufer"] = saeubern(m.group(1))
	d = finde_datum(text)
	b["datum"] = f"{d:%Y-%m-%d}" if d else ""

	m = re.search(rf"(?:Summe|Total)\s*:?\s*({RE_ZAHL})\s*([€$£])?\s*([A-Z]{{3}})?", text)
	if m:
		try:
			b["betrag"] = zahl(m.group(1))
			b["waehrung"] = m.group(3) or SYMBOLE.get(m.group(2) or "", "")
		except ValueError:
			pass
	# "139,90 € EUR = 147,99 $ USD"
	m = re.search(rf"({RE_ZAHL})\s*€?\s*EUR\s*=\s*({RE_ZAHL})\s*[^\d\s]?\s*([A-Z]{{3}})", text)
	if m:
		try:
			b["betrag_eur"] = zahl(m.group(1))
			if b["betrag"] is None:
				b["betrag"], b["waehrung"] = zahl(m.group(2)), m.group(3)
		except ValueError:
			pass
	m = re.search(rf"1\s*EUR\s*=\s*({RE_ZAHL})\s*[^\d\s]?\s*([A-Z]{{3}})", text)
	if m:
		try:
			b["kurs"] = zahl(m.group(1))
		except ValueError:
			pass
	if b["betrag_eur"] is None and b["waehrung"] == "EUR":
		b["betrag_eur"] = b["betrag"]
	return b


# ---------------------------------------------------------------- Gesamtauswertung

def analysiere(pfad, bekannte, eigene):
	"""Liest eine Rechnung aus: datum, lieferant, nummer, betrag, ust_ids, quelle, grund."""
	info = {"datum": "", "lieferant": "", "nummer": "", "betrag": None, "ust_ids": [],
		"quelle": "PDF-Text", "grund": ""}
	try:
		text, xml = pdf_lesen(pfad)
	except Exception as fehler:
		info["grund"] = f"PDF nicht lesbar ({fehler})"
		return info
	info["paypal"] = paypal_beleg(text)

	if xml:
		ex = lies_factur_x(xml)
		if ex:
			info["quelle"] = "E-Rechnung (ZUGFeRD/Factur-X)"
			info["datum"] = ex.get("datum", "")
			info["nummer"] = saeubern(ex.get("nummer", ""))
			info["nummer_roh"] = ex.get("nummer", "")
			info["betrag"] = ex.get("betrag")
			info["waehrung"] = ex.get("waehrung", "")
			if ex.get("ust_id"):
				info["ust_ids"] = [ex["ust_id"]]
			# Name aus lieferanten.txt hat Vorrang (kurze Namen im Dateinamen)
			kurz = finde_lieferant(text, bekannte, eigene) if text else None
			info["lieferant"] = saeubern(kurz or ex.get("lieferant", ""))

	if len(text.strip()) < 20 and not info["nummer"]:
		info["grund"] = "Kein Text im PDF – vermutlich ein Scan. Bitte Angaben von Hand ergänzen."
		return info

	if not info["datum"]:
		d = finde_datum(text)
		info["datum"] = f"{d:%Y-%m-%d}" if d else ""
	if not info["lieferant"]:
		lf = finde_lieferant(text, bekannte, eigene)
		info["lieferant"] = saeubern(lf) if lf else ""
	if not info["nummer"]:
		nr = finde_nummer(text)
		info["nummer"] = saeubern(nr) if nr else ""
	if info["betrag"] is None:
		info["betrag"] = finde_betrag(text)
	if not info.get("waehrung"):
		info["waehrung"] = finde_waehrung(text)
	if not info["ust_ids"]:
		info["ust_ids"] = finde_ust_ids(text)

	fehlend = [n for n, w in (("Datum", info["datum"]), ("Lieferant", info["lieferant"]),
		("Rechnungsnummer", info["nummer"])) if not w]
	if fehlend:
		info["grund"] = "Nicht erkannt: " + ", ".join(fehlend)
	return info


# ---------------------------------------------------------------- Dateinamen

def saeubern(wert):
	wert = unicodedata.normalize("NFC", wert or "")
	wert = re.sub(r'[\\/:*?"<>|]', "-", wert)
	wert = re.sub(r"\s+", " ", wert).strip(" .-")
	return wert


def zielname(datum, lieferant, nummer, kuerzel=""):
	name = f"{datum} - Rechnung - {saeubern(lieferant)} - {saeubern(nummer)}"
	if kuerzel:
		name += f" - {saeubern(kuerzel)}"
	return name + ".pdf"


def zerlege_namen(dateiname, kuerzel_liste=()):
	"""'2026-11-10 - Rechnung - Amazon - LU66 - MHE.pdf' -> dict(datum, lieferant, nummer, kuerzel)."""
	if not ZIELMUSTER.match(dateiname):
		return None
	stamm = dateiname[:-4]
	stamm = re.sub(r" \(\d+\)$", "", stamm)  # "(2)" aus freier_name
	teile = stamm.split(" - ")
	datum, _, rest = teile[0], teile[1], teile[2:]
	kuerzel = ""
	if len(rest) >= 3 and rest[-1] in kuerzel_liste:
		kuerzel = rest.pop()
	if len(rest) < 2:
		return None
	# Lieferant darf " - " enthalten -> Nummer ist immer das letzte Teil
	return {"datum": datum, "lieferant": " - ".join(rest[:-1]), "nummer": rest[-1], "kuerzel": kuerzel}


def belegname(datum, lieferant, nummer, kuerzel=""):
	return zielname(datum, lieferant, nummer, kuerzel).replace(" - Rechnung - ", " - Zahlungsbestätigung PayPal - ", 1)


def zerlege_belegnamen(dateiname, kuerzel_liste=()):
	"""Wie zerlege_namen, für '… - Zahlungsbestätigung PayPal - …'."""
	if not ist_belegname(dateiname):
		return None
	name = re.sub(r" - Zahlungsbestätigung PayPal - ", " - Rechnung - ",
		unicodedata.normalize("NFC", dateiname), count=1, flags=re.IGNORECASE)
	return zerlege_namen(name, kuerzel_liste)


def freier_name(ordner, wunsch):
	ziel = Path(ordner) / wunsch
	n = 2
	while ziel.exists():
		ziel = Path(ordner) / f"{Path(wunsch).stem} ({n}).pdf"
		n += 1
	return ziel


def ausgelassen(name):
	return name.startswith((".", "@", "#")) or name.lower() in {"$recycle.bin", "papierkorb"}


def pdf_dateien(ordner):
	ordner = Path(ordner)
	return sorted(
		(d for d in ordner.rglob("*")
		 if d.suffix.lower() == ".pdf"
		 and not any(ausgelassen(t) for t in d.relative_to(ordner).parts)
		 and d.is_file()),
		key=lambda d: str(d.relative_to(ordner)).lower(),
	)


def nummer_schluessel(nummer):
	"""Vergleichsschlüssel für Rechnungsnummern ('2026/102658' == '2026-102658')."""
	return re.sub(r"[^0-9a-z]", "", (nummer or "").lower())
