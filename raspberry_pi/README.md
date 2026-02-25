# Bambuddy Middleware - Raspberry Pi 3D Printer Bridge

Macht jeden USB-verbundenen G-Code 3D-Drucker für BambuLab Software sichtbar.
Läuft als Middleware auf einem Raspberry Pi zwischen dem 3D-Drucker (USB-Kabel) und der BambuLab Verwaltungssoftware (Netzwerk).

## Funktionsweise

```
  Bambu Studio / OrcaSlicer (PC)
         │
    ┌────┴─────────── Netzwerk (WLAN/LAN) ───────────┐
    │                                                  │
    │    Raspberry Pi (Middleware)                      │
    │    ┌─────────────────────────────────────┐       │
    │    │  SSDP   (Port 2021) ← Erkennung    │       │
    │    │  Bind   (Port 3000) ← Handshake    │       │
    │    │  MQTT   (Port 8883) ← Befehle/TLS  │       │
    │    │  FTP    (Port 9990) ← Dateiupload   │       │
    │    │                                     │       │
    │    │  GCodeMiddleware ← Übersetzung      │       │
    │    │       ↓                             │       │
    │    │  SerialConnection                   │       │
    │    └───────────┬─────────────────────────┘       │
    │                │                                  │
    └────────────────┘                                  │
                     │ USB-Kabel                        │
                     ▼                                  │
              3D-Drucker (Marlin/Klipper/etc.)         │
```

## Features

- **Temperatursteuerung**: Düse, Bett, Kammer (M104, M140, M141)
- **Lüftersteuerung**: Bauteil-, Hilfs-, Kammerlüfter (M106/M107)
- **Druckgeschwindigkeit**: BambuLab Speed-Level → M220
- **Druck-Steuerung**: Start, Pause, Fortsetzen, Abbrechen
- **Echtzeit-Status**: Temperaturen, Fortschritt, Layer-Tracking
- **3MF-Unterstützung**: G-Code Extraktion aus 3MF-Dateien
- **Kammerlicht**: Ein/Aus (M355)
- **G-Code Durchleitung**: Direkte Befehle vom Slicer zum Drucker

## Schnellinstallation

### Voraussetzungen

- Raspberry Pi (3B+, 4, 5 oder Zero 2W) mit Raspbian/Raspberry Pi OS
- Python 3.11 oder neuer
- 3D-Drucker per USB verbunden
- Netzwerkverbindung (WLAN oder LAN)

### Automatische Installation

```bash
# 1. Dateien auf den Raspberry Pi kopieren (vom PC aus):
scp -r raspberry_pi/* pi@raspberrypi:~/bambuddy-middleware/

# 2. Auf dem Raspberry Pi:
ssh pi@raspberrypi

cd ~/bambuddy-middleware
chmod +x install.sh
sudo ./install.sh
```

### Manuelle Installation

```bash
# 1. Abhängigkeiten installieren
pip3 install pyserial cryptography

# 2. Dateien kopieren
sudo mkdir -p /opt/bambuddy-middleware
sudo cp *.py /opt/bambuddy-middleware/
sudo chmod +x /opt/bambuddy-middleware/bambuddy_middleware.py

# 3. Konfiguration erstellen
sudo mkdir -p /etc/bambuddy-middleware
sudo nano /etc/bambuddy-middleware/config.json

# 4. Service installieren
sudo cp bambuddy-middleware.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bambuddy-middleware
sudo systemctl start bambuddy-middleware
```

## Konfiguration

### Konfigurationsdatei

Die Konfiguration liegt unter `/etc/bambuddy-middleware/config.json`:

```json
{
    "access_code": "12345678",
    "printer_name": "Bambuddy Middleware",
    "serial": "00M09A391800001",
    "model": "3DPrinter-X1-Carbon",
    "serial_port": "/dev/ttyUSB0",
    "baudrate": 115200,
    "data_dir": "/var/lib/bambuddy-middleware",
    "log_level": "INFO"
}
```

### Parameter

| Parameter | Beschreibung | Standard |
|---|---|---|
| `access_code` | Zugangscode für Slicer-Authentifizierung | `12345678` |
| `printer_name` | Name im Slicer | `Bambuddy Middleware` |
| `serial` | Seriennummer (SSDP/MQTT/Zertifikate) | `00M09A391800001` |
| `model` | Druckermodell-Code | `3DPrinter-X1-Carbon` |
| `serial_port` | USB-Port des Druckers | `/dev/ttyUSB0` |
| `baudrate` | Baudrate für serielle Kommunikation | `115200` |
| `data_dir` | Verzeichnis für Zertifikate und Uploads | `/var/lib/bambuddy-middleware` |
| `log_level` | Log-Level (DEBUG/INFO/WARNING/ERROR) | `INFO` |

### Modell-Codes

| Code | Drucker |
|---|---|
| `3DPrinter-X1-Carbon` | X1 Carbon (beste Kompatibilität) |
| `C11` | P1S |
| `C12` | P1P |
| `N1` | A1 |

### Seriellen Port finden

```bash
# Alle verfügbaren Ports anzeigen
python3 bambuddy_middleware.py --list-ports

# Oder manuell
ls /dev/tty*
```

Typische Ports:
- `/dev/ttyUSB0` - USB-Serial Adapter (z.B. CH340, CP2102)
- `/dev/ttyACM0` - Arduino/Native USB (z.B. Prusa, Ender 3 V2)

## Betrieb

### Service starten/stoppen

```bash
# Status prüfen
sudo systemctl status bambuddy-middleware

# Log anzeigen (live)
sudo journalctl -u bambuddy-middleware -f

# Stoppen
sudo systemctl stop bambuddy-middleware

# Starten
sudo systemctl start bambuddy-middleware

# Neustarten (nach Konfigurationsänderung)
sudo systemctl restart bambuddy-middleware
```

### Manueller Start (zum Testen)

```bash
cd /opt/bambuddy-middleware
sudo python3 bambuddy_middleware.py --serial-port /dev/ttyUSB0 --log-level DEBUG
```

### Kommandozeilen-Optionen

```bash
python3 bambuddy_middleware.py --help

# Beispiele:
python3 bambuddy_middleware.py --serial-port /dev/ttyACM0 --baudrate 250000
python3 bambuddy_middleware.py --access-code geheim12 --printer-name "Ender 3"
python3 bambuddy_middleware.py --config ./meine-config.json
```

### Umgebungsvariablen

```bash
export MIDDLEWARE_SERIAL_PORT=/dev/ttyACM0
export MIDDLEWARE_BAUDRATE=250000
export MIDDLEWARE_ACCESS_CODE=meincode
export MIDDLEWARE_DATA_DIR=/tmp/middleware-data
```

## Im Slicer einrichten

1. **Bambu Studio / OrcaSlicer** starten
2. Der Drucker sollte automatisch in der Netzwerksuche erscheinen
3. Drucker auswählen und **Zugangscode** eingeben (aus config.json)
4. Drucken!

Falls der Drucker nicht automatisch erscheint:
- Prüfen ob der Raspberry Pi im selben Netzwerk ist
- Port 2021 (SSDP), 3000 (Bind), 8883 (MQTT), 9990 (FTP) in der Firewall freigeben
- Logs prüfen: `sudo journalctl -u bambuddy-middleware -f`

## Dateien

| Datei | Beschreibung |
|---|---|
| `bambuddy_middleware.py` | Hauptprogramm und Einstiegspunkt |
| `serial_connection.py` | Serielle Kommunikation mit dem Drucker |
| `gcode_middleware.py` | BambuLab MQTT ↔ G-Code Übersetzung |
| `mqtt_server.py` | MQTT-Broker für Slicer-Kommunikation |
| `ftp_server.py` | FTPS-Server für Dateiuploads |
| `ssdp_server.py` | SSDP für Druckererkennung |
| `bind_server.py` | Bind/Detect für Slicer-Handshake |
| `certificate.py` | TLS-Zertifikatgenerierung |
| `bambuddy-middleware.service` | systemd Service-Datei |
| `install.sh` | Installationsskript |
| `requirements.txt` | Python-Abhängigkeiten |

## Fehlerbehebung

### Drucker nicht verbunden
```
Could not connect to printer on /dev/ttyUSB0
```
→ USB-Kabel prüfen, `--list-ports` verwenden, Baudrate prüfen

### Port bereits belegt
```
MQTT port 8883 is already in use
```
→ Anderen Dienst auf dem Port stoppen oder Middleware neu starten

### Berechtigung verweigert
```
Permission denied for port /dev/ttyUSB0
```
→ User zur `dialout` Gruppe hinzufügen: `sudo usermod -a -G dialout $USER`

### Slicer findet Drucker nicht
→ Firewall-Ports prüfen (2021, 3000, 8883, 9990)
→ Raspberry Pi und PC müssen im selben Netzwerk sein
→ SSDP Broadcasts müssen erlaubt sein (kein Client Isolation im Router)
