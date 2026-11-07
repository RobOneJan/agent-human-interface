# Rechnung aus E-Mail ablegen

Gilt, wenn eine Lieferantenrechnung aus einer E-Mail nach Odoo soll ("leg die
Rechnung ab", "Rechnung von X einpflegen", ...).

1. **Rechnung holen.** Die E-Mail suchen und den Rechnungsanhang mit
   `get_attachment` abrufen (PDF oder XML). Mehrere Rechnungen in einer Mail
   nacheinander bearbeiten.

2. **Ablegen** mit `terbox__odoo_rechnung_ablegen`. Immer mitgeben:
   - `anhaenge`: die `attachment_ref` des Anhangs, nie den Inhalt selbst
   - `lieferant_name`, `ust_id`, `rechnungsnummer`, `rechnungsdatum`,
     `faelligkeitsdatum`, so wie sie auf der Rechnung stehen
   - `betraege`: Nettosumme je Steuersatz. **Immer angeben**, auch bei einer
     E-Rechnung. Eine Rechnung wird nie ohne Betrag abgelegt.
   - `kontakt` (E-Mail, Telefon, Adresse aus Rechnung oder Mail-Signatur) und
     `iban`, wenn sichtbar
   - `kategorie`: die Kostenart für das interne Reporting, wenn klar ist, wofür
     das Geld ausgegeben wurde. Genau einer dieser Namen:
     - Einzelkosten (fließt direkt in unsere Produkte): Material, Fremdleistung
     - Gemeinkosten: Werkzeuge & Betriebsmittel, Fahrzeuge & Kraftstoff,
       Transport & Versand, Marketing, IT & Software, Büro & Verwaltung,
       Gebäude & Miete, Beratung & Dienstleistungen, Sonstiges

     Bei gemischten Rechnungen die überwiegende Kostenart. Im Zweifel weglassen.

   Nie Einzelpositionen als Rechnungszeilen anlegen. Das Werkzeug legt eine
   Summenzeile je Steuersatz an.

3. **Ergebnis auswerten** (`ergebnis`):
   - `abgelegt`: der Entwurf liegt in Odoo, eine Karte fragt, ob gebucht werden
     soll. Weiter mit Schritt 4. Nicht selbst buchen; das passiert erst mit der
     Freigabe auf der Karte.
   - `neuer_lieferant`: der Lieferant ist neu. Es gibt **eine** Karte, die
     Lieferant anlegen, Rechnung ablegen und buchen zusammen freigibt. Nicht
     zusätzlich `odoo_lieferant_anlegen` aufrufen. Schritt 4 entfällt.
   - `bereits_vorhanden` / `bereits_abgelegt`: die Rechnung ist schon in Odoo.
     Nur die `meldung` weitergeben (sie nennt den Lieferanten). Fertig.
   - `nummer_bei_anderem_lieferanten`: kurz nachfragen, nichts weiter tun.
   - `lieferant_unklar`: kurz nachfragen, welcher der `kandidaten` gemeint ist.
   - Fehler `betraege missing`: Summen aus der Rechnung lesen und erneut aufrufen.

4. **Preise der Lieferantenpreisliste ergänzen**, nur wenn der Lieferant
   bekannt ist und die Artikel eindeutig sind:
   - Die Positionen kommen bei E-Rechnungen als `positionen` zurück; sonst nur
     Positionen verwenden, die klar lesbar sind (Artikelnummer, Einzelpreis).
   - `terbox__odoo_preise_pruefen` mit der `lieferant_id` und diesen Positionen.
   - Alle Treffer mit Status `abweichung` oder `kein_preis` in **einem**
     Aufruf `terbox__odoo_einkaufspreise_aendern` (eine Freigabekarte).
   - Weglassen: `nicht_gefunden`, Positionen ohne eindeutige Artikelnummer,
     Positionen, deren Einheit offensichtlich nicht zur Einheit in Odoo passt
     (z. B. Meter statt Stück, Packung statt Stück), Rabatt- und Versandzeilen.
   - Gibt es nichts Eindeutiges, Schritt 4 einfach auslassen.

5. **Abweichende Kontaktdaten**: kommt `abweichende_kontaktdaten` zurück,
   `terbox__odoo_lieferant_aktualisieren` mit den neuen Werten aufrufen
   (Freigabe). Bankdaten nie ändern.

6. **Antwort im Chat**: genau die `meldung` aus Schritt 2, ggf. ergänzt um
   "Preisfreigabe für N Artikel angefragt." Keine Positionslisten, keine
   Zusammenfassung der Rechnung. Gebucht wird nur über die Freigabekarte,
   bezahlt wird nie.
