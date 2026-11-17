# DF-Handbuch als PDF erstellen

Die fertige Datei ist [KrakenDF-Funktion-und-Bedienung.pdf](KrakenDF-Funktion-und-Bedienung.pdf).
Inhalt: [df-handbuch.md](df-handbuch.md); Layout: [df-handbuch-layout.tex](df-handbuch-layout.tex).
Das Dokument beschreibt den im Titel genannten Softwarestand. Die Erklärgrafiken
sind keine realen RF-Messungen. Die Arraygrafik wird aus den eingecheckten
Antennenprofilen erzeugt.

Zum Neuerstellen unter Debian/Ubuntu oder WSL:

```bash
sudo apt-get install pandoc texlive-xetex texlive-latex-extra fonts-dejavu-core
python3 -m venv /tmp/krakendf-docs-venv
/tmp/krakendf-docs-venv/bin/python -m pip install matplotlib
/tmp/krakendf-docs-venv/bin/python scripts/build-df-handbook.py
```

Den letzten Befehl im Projektverzeichnis ausführen. Das Skript erstellt die
fünf PNG-Grafiken unter `docs/abbildungen/` und das PDF neu. Matplotlib,
Pandoc und LaTeX werden nur für die Dokumenterstellung benötigt; zum Lesen
des PDFs oder für den App-Betrieb sind sie nicht erforderlich. Unter einem
nativen Windows-Python genügt dieselbe Werkzeugausstattung, für einen
einfachen einheitlichen Build empfiehlt sich WSL.

Nach Inhaltsänderungen das PDF auf überlaufende Tabellen, zusätzliche
Restseiten, korrekte Winkelkonventionen und lesbare Grafiken prüfen. Der
Quellstand im Handbuch bezeichnet die dokumentierte Implementierung,
nicht den späteren Commit, der das Handbuch ergänzt.
