// Eingangsrechnung Import: beim Öffnen der Liste "Eingangsrechnung" den
// Rechnungsordner prüfen, Umbenennen anbieten und Entwürfe anlegen.
frappe.provide("erpnext_eingangsrechnung");

(function () {
	const einstellungen = (frappe.listview_settings["Purchase Invoice"] =
		frappe.listview_settings["Purchase Invoice"] || {});
	const altes_onload = einstellungen.onload;

	einstellungen.onload = function (listview) {
		if (altes_onload) altes_onload.apply(this, arguments);
		if (!frappe.model.can_create("Purchase Invoice")) return;

		listview.page.add_menu_item(__("Rechnungsordner prüfen"), () =>
			erpnext_eingangsrechnung.start(listview, true)
		);
		if (frappe.user.has_role(["System Manager", "Accounts Manager"])) {
			listview.page.add_menu_item(__("Einstellungen Rechnungsordner"), () =>
				frappe.set_route("Form", "Eingangsrechnung Import Einstellungen")
			);
		}
		erpnext_eingangsrechnung.start(listview, false);
	};
})();

erpnext_eingangsrechnung.start = function (listview, manuell) {
	if (erpnext_eingangsrechnung.laeuft) return;
	erpnext_eingangsrechnung.laeuft = true;
	frappe.call({
		method: "erpnext_eingangsrechnung.api.pruefen",
		args: { manuell: manuell ? 1 : 0 },
		freeze: manuell,
		freeze_message: __("Rechnungsordner wird geprüft …"),
		callback(r) {
			const d = r.message || {};
			if (!d.aktiv) {
				erpnext_eingangsrechnung.laeuft = false;
				if (manuell && d.fehler) frappe.msgprint(d.fehler);
				return;
			}
			if (d.fehler) {
				erpnext_eingangsrechnung.laeuft = false;
				if (manuell) frappe.msgprint(d.fehler);
				else frappe.show_alert({ message: d.fehler, indicator: "orange" }, 8);
				return;
			}
			if (d.umbenennen && d.umbenennen.length) {
				erpnext_eingangsrechnung.dialog(listview, d, manuell);
			} else {
				erpnext_eingangsrechnung.erfassen(listview, manuell);
			}
		},
		error() {
			erpnext_eingangsrechnung.laeuft = false;
		},
	});
};

erpnext_eingangsrechnung.dialog = function (listview, daten, manuell) {
	const esc = frappe.utils.escape_html;
	const zahler_optionen = ['<option value="">—</option>']
		.concat(
			(daten.zahler || []).map(
				(z) => `<option value="${esc(z.kuerzel)}">${esc(z.kuerzel)} – ${esc(z.bezeichnung)}</option>`
			)
		)
		.join("");

	const zeilen = daten.umbenennen
		.map((f, i) => {
			const link =
				"/api/method/erpnext_eingangsrechnung.api.datei_anzeigen?pfad=" + encodeURIComponent(f.pfad);
			const betrag = f.betrag != null ? format_currency(f.betrag, "EUR") : "";
			return `
			<tr data-i="${i}">
				<td style="max-width:260px">
					<a href="${link}" target="_blank" rel="noopener">${esc(f.datei)}</a>
					<div class="text-muted small">${esc(f.ordner)}</div>
					${f.art === "paypal" ? `<span class="indicator-pill blue">${__("PayPal-Zahlungsbeleg")}</span>` : ""}
					${f.grund ? `<div class="small" style="color:${f.art === "paypal" ? "var(--blue-600)" : "var(--red-600)"}">${esc(f.grund)}</div>` : ""}
					${betrag ? `<div class="text-muted small">${__("Betrag")}: ${betrag}</div>` : ""}
				</td>
				<td><select class="form-control input-xs eri-aktion">
					<option value="umbenennen">${__("Umbenennen")}</option>
					<option value="ueberspringen">${__("Später")}</option>
					<option value="ignorieren">${__("Keine Rechnung")}</option>
				</select></td>
				<td><input type="date" class="form-control input-xs eri-datum" value="${esc(f.datum)}"></td>
				<td><input type="text" class="form-control input-xs eri-lieferant" value="${esc(f.lieferant)}"></td>
				<td><input type="text" class="form-control input-xs eri-nummer" value="${esc(f.nummer)}"></td>
				<td><select class="form-control input-xs eri-kuerzel">${zahler_optionen}</select></td>
			</tr>`;
		})
		.join("");

	const dialog = new frappe.ui.Dialog({
		title: __("Neue Rechnungen im Ordner ({0})", [daten.umbenennen.length]),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "tabelle",
				options: `
				<p class="text-muted">${__(
					"Diese PDFs sind noch nicht nach dem Schema „JJJJ-MM-TT - Rechnung - Lieferant - Nummer“ benannt. Bitte prüfen und bestätigen. Danach werden Entwürfe angelegt (nicht gebucht)."
				)}</p>
				<p class="text-muted small">${__(
					"PayPal-Zahlungsbelege werden als „JJJJ-MM-TT - Zahlungsbestätigung PayPal - Lieferant - Rechnungsnummer“ benannt, an die passende Rechnung gehängt und nie als eigene Rechnung angelegt."
				)}</p>
				<div style="overflow-x:auto"><table class="table table-bordered table-sm">
					<thead><tr>
						<th>${__("Datei")}</th><th>${__("Aktion")}</th><th>${__("Rechnungsdatum")}</th>
						<th>${__("Lieferant")}</th><th>${__("Rechnungsnummer")}</th><th>${__("Privat bezahlt")}</th>
					</tr></thead>
					<tbody>${zeilen}</tbody>
				</table></div>`,
			},
		],
		primary_action_label: __("Ausführen"),
		primary_action() {
			const auftraege = [];
			dialog.$wrapper.find("tbody tr").each(function () {
				const tr = $(this);
				const f = daten.umbenennen[tr.data("i")];
				auftraege.push({
					pfad: f.pfad,
					art: f.art || "",
					aktion: tr.find(".eri-aktion").val(),
					datum: tr.find(".eri-datum").val(),
					lieferant: tr.find(".eri-lieferant").val(),
					nummer: tr.find(".eri-nummer").val(),
					kuerzel: tr.find(".eri-kuerzel").val(),
				});
			});
			frappe.call({
				method: "erpnext_eingangsrechnung.api.umbenennen",
				args: { auftraege: auftraege },
				freeze: true,
				freeze_message: __("Dateien werden umbenannt …"),
				callback(r) {
					const fehler = (r.message || []).filter((x) => !x.ok);
					dialog.hide();
					if (fehler.length) {
						frappe.msgprint({
							title: __("Nicht umbenannt"),
							indicator: "orange",
							message: fehler.map((x) => `${esc(x.pfad)}: ${esc(x.meldung)}`).join("<br>"),
						});
					}
					erpnext_eingangsrechnung.erfassen(listview, true);
				},
			});
		},
		secondary_action_label: __("Später"),
		secondary_action() {
			dialog.hide();
			// Ohne Umbenennen trotzdem die fertig benannten Dateien erfassen
			erpnext_eingangsrechnung.erfassen(listview, manuell);
		},
	});
	dialog.onhide = () => {
		erpnext_eingangsrechnung.laeuft = false;
	};
	dialog.show();
};

erpnext_eingangsrechnung.erfassen = function (listview, manuell, vorher) {
	erpnext_eingangsrechnung.laeuft = true;
	frappe.call({
		method: "erpnext_eingangsrechnung.api.erfassen",
		freeze: manuell,
		freeze_message: __("Entwürfe werden angelegt …"),
		callback(r) {
			erpnext_eingangsrechnung.laeuft = false;
			const b = r.message || {};
			// Ergebnis eines früheren Laufs (vor dem Anlegen neuer Lieferanten) mit anzeigen
			if (vorher) {
				["angelegt", "vorhanden", "verschoben", "belege"].forEach((k) => {
					b[k] = (vorher[k] || []).concat(b[k] || []);
				});
			}
			const fehlt = b.lieferant_fehlt || [];
			if (fehlt.length && !vorher) {
				erpnext_eingangsrechnung.lieferanten_anlegen(fehlt, 0, 0, () =>
					erpnext_eingangsrechnung.erfassen(listview, true, b)
				);
				return;
			}
			erpnext_eingangsrechnung.ergebnis(listview, manuell, b);
		},
		error() {
			erpnext_eingangsrechnung.laeuft = false;
		},
	});
};

erpnext_eingangsrechnung.ergebnis = function (listview, manuell, b) {
	const esc = frappe.utils.escape_html;
	const link = (name) => `<a href="/app/purchase-invoice/${encodeURIComponent(name)}">${esc(name)}</a>`;
	const teile = [];
	if ((b.angelegt || []).length) {
		teile.push(
			`<b>${__("Neue Entwürfe")}</b><br>` +
				b.angelegt
					.map(
						(x) =>
							`${link(x.rechnung)} – ${esc(x.lieferant)} – ${
								x.betrag != null ? format_currency(x.betrag, x.waehrung || "EUR") : __("Betrag fehlt")
							}${x.hinweis ? `<div class="small" style="color: var(--orange-600, #c05621)">${esc(x.hinweis)}</div>` : ""}<div class="text-muted small">${esc(x.datei)}</div>`
					)
					.join("")
		);
	}
	if ((b.vorhanden || []).length) {
		teile.push(
			`<b>${__("Schon in ERPNext")}</b><br>` +
				b.vorhanden.map((x) => `${link(x.rechnung)} – ${esc(x.hinweis || "")}`).join("<br>")
		);
	}
	if ((b.belege || []).length) {
		teile.push(
			`<b>${__("PayPal-Zahlungsbelege")}</b><br>` +
				b.belege
					.map(
						(x) =>
							`${x.rechnung ? link(x.rechnung) : __("ohne Rechnung")} – ${esc(x.neu || x.datei)}${
								x.hinweis ? `<div class="text-muted small">${esc(x.hinweis)}</div>` : ""
							}`
					)
					.join("<br>")
		);
	}
	if ((b.verschoben || []).length) {
		teile.push(
			`<b>${__("Bezahlt und verschoben")}</b><br>` +
				b.verschoben.map((x) => `${link(x.rechnung)} → ${esc(x.nach)}`).join("<br>")
		);
	}
	if ((b.fehler || []).length) {
		teile.push(
			`<b style="color:var(--orange-600)">${__("Noch offen")}</b><br>` +
				b.fehler.map((x) => `${esc(x.datei)}: ${esc(x.meldung)}`).join("<br>")
		);
	}
	if (teile.length) {
		frappe.msgprint({
			title: __("Rechnungsordner – {0}", [b.zusammenfassung || ""]),
			message: teile.join("<hr>"),
			wide: true,
		});
		listview.refresh();
	} else if (manuell) {
		frappe.show_alert({ message: __("Keine neuen Rechnungen im Ordner."), indicator: "green" });
	}
};

// Fehlende Lieferanten nacheinander zum Anlegen anbieten; danach fertig() aufrufen
erpnext_eingangsrechnung.lieferanten_anlegen = function (liste, i, angelegt, fertig) {
	if (i >= liste.length) {
		if (angelegt) fertig();
		else fertig();
		return;
	}
	const eintrag = liste[i];
	const v = eintrag.vorschlag || {};
	const esc = frappe.utils.escape_html;
	const pdf = "/api/method/erpnext_eingangsrechnung.api.datei_anzeigen?pfad=" + encodeURIComponent(v.pfad || eintrag.dateien[0]);
	const dialog = new frappe.ui.Dialog({
		title: __("Lieferant „{0}“ anlegen?", [eintrag.name]),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "info",
				options: `<p>${__(
					"Diesen Lieferanten gibt es in ERPNext noch nicht. Betroffen: {0} Rechnung(en).",
					[eintrag.dateien.length]
				)} <a href="${pdf}" target="_blank" rel="noopener">${__("Rechnung ansehen")}</a></p>
				<p class="text-muted small">${__(
					"Die Daten wurden aus der Rechnung gelesen ({0}). Bitte prüfen und ergänzen.",
					[esc(v.quelle || "")]
				)}</p>`,
			},
			{ fieldtype: "Section Break", label: __("Firma") },
			{ fieldname: "supplier_name", fieldtype: "Data", label: __("Name"), reqd: 1, default: v.name },
			{ fieldname: "supplier_group", fieldtype: "Link", options: "Supplier Group", label: __("Lieferantengruppe"), default: v.lieferantengruppe },
			{ fieldname: "tax_id", fieldtype: "Data", label: __("USt-IdNr. / Tax ID"), default: v.tax_id },
			{ fieldname: "tax_category", fieldtype: "Link", options: "Tax Category", label: __("Steuerkategorie"), default: v.steuerkategorie },
			{ fieldtype: "Column Break" },
			{ fieldname: "payment_terms", fieldtype: "Link", options: "Payment Terms Template", label: __("Zahlungsbedingung"), default: v.zahlungsbedingung },
			{ fieldname: "default_currency", fieldtype: "Link", options: "Currency", label: __("Standardwährung"), default: v.standardwaehrung,
				description: v.waehrung && v.waehrung !== v.standardwaehrung ? __("Rechnung ist in {0}. Feld leer lassen – die Rechnung kann trotzdem in {0} sein.", [v.waehrung]) : "" },
			{ fieldname: "kreditor", fieldtype: "Data", label: __("Kreditorennummer"), default: v.kreditor, hidden: v.kreditor_moeglich ? 0 : 1 },
			{ fieldname: "website", fieldtype: "Data", label: __("Webseite"), default: v.website },
			{ fieldtype: "Section Break", label: __("Adresse") },
			{ fieldname: "strasse", fieldtype: "Data", label: __("Straße"), default: v.strasse },
			{ fieldname: "adresszusatz", fieldtype: "Data", label: __("Adresszusatz"), default: v.adresszusatz },
			{ fieldname: "plz", fieldtype: "Data", label: __("PLZ"), default: v.plz },
			{ fieldname: "ort", fieldtype: "Data", label: __("Ort"), default: v.ort },
			{ fieldtype: "Column Break" },
			{ fieldname: "bundesland", fieldtype: "Data", label: __("Bundesland / Provinz"), default: v.bundesland },
			{ fieldname: "land", fieldtype: "Link", options: "Country", label: __("Land"), default: v.land },
			{ fieldname: "email", fieldtype: "Data", options: "Email", label: __("E-Mail"), default: v.email },
			{ fieldname: "telefon", fieldtype: "Data", label: __("Telefon"), default: v.telefon },
			{ fieldtype: "Section Break", label: __("Bankverbindung"), collapsible: v.iban ? 0 : 1 },
			{ fieldname: "iban", fieldtype: "Data", label: __("IBAN"), default: v.iban },
			{ fieldname: "bic", fieldtype: "Data", label: __("BIC"), default: v.bic },
			{ fieldtype: "Column Break" },
			{ fieldname: "bank", fieldtype: "Data", label: __("Bank"), default: v.bank,
				description: __("Ohne Banknamen wird die IBAN nur in den Details vermerkt.") },
			{ fieldname: "kontoinhaber", fieldtype: "Data", label: __("Kontoinhaber"), default: v.kontoinhaber },
			{ fieldtype: "Section Break" },
			{ fieldname: "notiz", fieldtype: "Small Text", label: __("Notiz (Lieferanten-Details)"), default: v.notiz },
		],
		primary_action_label: __("Lieferant anlegen"),
		primary_action(werte) {
			frappe.call({
				method: "erpnext_eingangsrechnung.api.lieferant_anlegen",
				args: { daten: werte, dateien: eintrag.dateien },
				freeze: true,
				freeze_message: __("Lieferant wird angelegt …"),
				callback(r) {
					const e = r.message || {};
					frappe.show_alert(
						{ message: __("Lieferant {0} angelegt.", [esc(e.lieferant || "")]), indicator: "green" },
						6
					);
					(e.hinweise || []).forEach((h) => frappe.show_alert({ message: esc(h), indicator: "orange" }, 10));
					dialog.weiter = true;
					dialog.hide();
					erpnext_eingangsrechnung.lieferanten_anlegen(liste, i + 1, angelegt + 1, fertig);
				},
			});
		},
		secondary_action_label: __("Überspringen"),
		secondary_action() {
			dialog.weiter = true;
			dialog.hide();
			erpnext_eingangsrechnung.lieferanten_anlegen(liste, i + 1, angelegt, fertig);
		},
	});
	dialog.onhide = () => {
		// Mit X geschlossen: wie Überspringen
		if (!dialog.weiter) erpnext_eingangsrechnung.lieferanten_anlegen(liste, i + 1, angelegt, fertig);
	};
	dialog.show();
};
