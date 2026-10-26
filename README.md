# Vimeo Library Downloader

Lädt alle Videos aus dem **eigenen** Vimeo-Account herunter – über die offizielle Vimeo-API, mit kleiner Oberfläche (GUI) oder per Kommandozeile.

## Funktionen

- Lädt die komplette Library (alle API-Seiten), auch private Videos
- Qualität wählbar: `best` = Originaldatei (Source), sonst max. Auflösung (2160 … 360)
- Baut die Vimeo-Ordnerstruktur auf Wunsch lokal nach
- Überspringt bereits geladene Videos, setzt abgebrochene Downloads (`.part`) fort
- Holt abgelaufene Download-Links automatisch neu, beachtet API-Limits
- **Testlauf** (`--dry-run`): geht die ganze Library durch, lädt aber nichts
- Nach jedem Lauf: nummerierte **Videoliste** (`Vimeo_Videoliste.txt` + `.csv` für Excel) und `vimeo_manifest.json`

## Voraussetzungen

- Python 3.9+ und `pip install requests`
- Vimeo-Plan **Standard oder höher** (sonst liefert die API keine Download-Links)
- Personal Access Token mit den Scopes **public, private, video_files**  
  → [developer.vimeo.com/apps](https://developer.vimeo.com/apps) → App anlegen → *Generate Access Token*

## Start

**Windows:** `Vimeo Downloader starten.bat` doppelklicken (installiert `requests` bei Bedarf und öffnet die GUI).

**GUI direkt:**
```
python vimeo_downloader.py
```

**Kommandozeile:**
```
python vimeo_downloader.py --token DEIN_TOKEN --out "D:/Vimeo Backup" --quality best
python vimeo_downloader.py --dry-run          # nur Liste erzeugen
```

Optionen: `--token`, `--out`, `--quality {best,2160,1440,1080,720,540,360}`, `--no-folders`, `--dry-run`, `--gui`.  
Der Token kann auch über die Umgebungsvariable `VIMEO_TOKEN` kommen.

## Dateinamen

```
<Zielordner>/<Vimeo-Ordner>/2024-05-01 Titel [123456789].mp4
```
Die Video-ID im Namen sorgt dafür, dass erneute Läufe Vorhandenes erkennen.

## Hinweise

- „Token merken“ in der GUI speichert den Token im Klartext unter `~/.vimeo_library_downloader.json`. Den Token nie ins Repository committen.
- Nur für eigene Inhalte gedacht.
