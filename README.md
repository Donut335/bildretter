# Bildretter

## 🌐 [Direkt im Browser benutzen – ohne Download](https://donut335.github.io/bildretter/)

Die Bilder werden nur im eigenen Browser bearbeitet und nicht hochgeladen. Ergebnis kommt als Zip
(`Gerettete Bilder <Datum>.zip`) in den Downloads-Ordner.

## ⬇️ [Desktop-Version herunterladen (Bildretter.zip)](https://github.com/Donut335/bildretter/releases/latest/download/Bildretter.zip)

Zip entpacken → `Bildretter starten.bat` doppelklicken. Ältere Versionen: [alle Releases](https://github.com/Donut335/bildretter/releases).

Windows-App zum Retten kaputter JPEG-Fotos, typischerweise gelöschte Bilder, die von einer
Speicherkarte wiederhergestellt wurden und sich nicht mehr öffnen lassen, weil der Dateikopf fehlt.

## Benutzung
1. Doppelklick auf **`Bildretter starten.bat`**. Beim allerersten Start wird kurz die
   Python-Umgebung eingerichtet.
2. Kaputte Dateien ins Fenster ziehen (oder „Dateien auswählen“).
3. Warten, bis das drehende Logo verschwindet („Bilder reparieren kann einige Minuten dauern“).
4. **„Fertige Bilder herunterladen“** → legt im Downloads-Ordner einen Ordner
   `Gerettete Bilder <Datum Uhrzeit>` mit den geretteten Bildern (PNG) an und öffnet ihn.

Intakte JPEGs werden unverändert mitkopiert. Dateien ohne erkennbare Bilddaten (z.B.
verschlüsselt) werden in der Ergebnisliste als nicht rettbar markiert.

## Was die Reparatur macht
Für Dateien, die nur noch JPEG-Bilddaten (mit Restart-Markern) enthalten:
- erkennt Farbabtastung (4:4:4 / 4:2:2 / 4:2:0) und Restart-Intervall durch Probe-Decodieren
  mit den Standard-Huffman-Tabellen,
- findet die Bildbreite über die Autokorrelation der Helligkeitswerte und die Startspalte über
  die Bildkante,
- berechnet die Quantisierungstabellen aus den Daten (glatte Übergänge an den 8×8-Blockgrenzen;
  Dateien mit gleichen Einstellungen werden gemeinsam ausgewertet),
- setzt einen passenden JPEG-Kopf davor und speichert nur die vollständigen Bildzeilen.

Code: `bildretter/engine.py` (Reparatur), `bildretter/app.py` (Oberfläche),
`bildretter/jpegscan.py` (Huffman-Decoder). Web-Version: `web/index.html` + `web/worker.js`
führen denselben Python-Code per Pyodide 0.29.5 im Browser aus; `.github/workflows/pages.yml`
veröffentlicht sie bei jedem Push auf GitHub Pages.

Web-Version lokal testen: `web/*` und `bildretter/{__init__,engine,jpegscan}.py` (nach `py/`)
in einen Ordner kopieren und dort `python -m http.server` starten (ein Worker braucht http://,
`file://` geht nicht). Entstanden aus `Sandbox/Bilder_Entschluesseln`.

## Voraussetzungen
- Windows, Python 3.10+ auf dem PATH (entwickelt mit 3.10.11)
- Pakete (werden von der .bat installiert): Pillow 12.3.0, numpy 2.2.6, tkinterdnd2 0.6.4

## Grenzen
- Gerettet wird nur, was in der Datei steckt; oft ist das nur ein Streifen des Fotos.
- Die Grundhelligkeit-Stufe wird aus dem Helligkeitsumfang geschätzt. Bei einem **einzelnen**
  kontrastarmen Bild kann sie um 1 daneben liegen (Bild etwas zu kontrastreich). Mehrere Bilder
  derselben Kamera gemeinsam hineinziehen macht die Schätzung sicher.
- Farbsättigung ist angenommen (Chroma-DC = Luma-DC).
- Dateien ohne Restart-Marker oder Graustufen-JPEGs werden (noch) nicht unterstützt.

## Status
- 2026-10-20: erste Version. Mit 6 Testdateien geprüft (4 gerettet, 2 korrekt als nicht rettbar
  erkannt), Oberfläche per Skript durchgespielt. Echtes Hineinziehen mit der Maus noch nicht getestet.
- 2026-10-20: Web-Version. Lokal im Browser mit denselben 6 Dateien geprüft (27 s, Ergebnisse
  identisch mit der Desktop-Version, Zip-Download geprüft).

**Git:** https://github.com/Donut335/bildretter (öffentlich, für GitHub Pages)
