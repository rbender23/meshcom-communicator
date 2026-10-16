# MeshCom Communicator

Ein interaktives Web-Dashboard und Backend für [MeshCom](https://meshcom.oevsv.at/) Nodes. Das Skript lauscht auf den `extUDP`-Port des Nodes, speichert alle Telemetrie- und Chat-Daten in einer lokalen SQLite-Datenbank und stellt sie visuell im Browser dar.

## Features
* **Live-Karte (Leaflet):** Zeichnet Nodes und aktive Mesh-Pfade (Routen) dynamisch auf einer Landkarte ein.
* **Chat-System:** Senden und Empfangen von Nachrichten an TGs (z. B. 26257), Direct Messages (DM) oder Broadcasts (*).
* **Heard Nodes:** Kompakte Übersicht aller empfangenen Stationen inkl. RSSI, SNR, Distanzberechnung und Firmware-Version. (Inaktive Nodes > 3h werden markiert).
* **@-Mentions:** Schnelles Erwähnen und Auswählen von Rufzeichen direkt aus der UI.
* **Pushover-Alarme:** Automatische Push-Benachrichtigungen aufs Handy bei eingehenden DMs, Namensnennungen oder Aktivität auf konfigurierten Ziel-TGs.
* **Live UDP Monitor:** Zeigt die rohen JSON-Pakete zur Fehlerdiagnose live im Browser.
* **Web-Konfiguration & Theme:** Alle Parameter (IPs, Ports, Rufzeichen, API-Keys) lassen sich bequem direkt im Browser anpassen. Integrierter Dark/Light-Mode.

## Installation

**1. Repository klonen**
```bash
git clone https://github.com/rbender23/meshcom-communicator.git
cd meshcom-communicator
```

**2. Abhängigkeiten installieren**
Das Skript benötigt das Web-Framework Flask.
```bash
pip3 install -r requirements.txt
```
*(Hinweis für neuere Raspberry Pi OS Versionen: Falls `pip3` einen Fehler bezüglich "externally managed environment" wirft, installiere Flask stattdessen systemweit über die Paketverwaltung mit `sudo apt install python3-flask`).*

**3. Konfiguration anlegen**
Kopiere die Vorlagen-Datei und benenne sie um:
```bash
cp app.ini.example app.ini
```
*Tipp: Die `app.ini` wird beim Start automatisch eingelesen. Du kannst die Werte für dein Funk-Setup danach direkt über den Reiter "⚙️ Einstellungen" im Web-Interface anpassen.*

**4. Anwendung manuell starten (Test)**
```bash
python3 app.py
```
Das Web-Interface ist nun lokal oder im Netzwerk unter `http://<IP-DEINES-PI>:5000` erreichbar. Mit `Strg+C` kannst du das Skript wieder beenden.

---

## Autostart (Systemd Service)
Um den Communicator dauerhaft und automatisch im Hintergrund laufen zu lassen, richte am besten einen Systemd-Dienst ein:

1. Erstelle die Service-Datei:
   ```bash
   sudo nano /etc/systemd/system/meshcom-communicator.service
   ```

2. Füge folgenden Inhalt ein *(Pfade ggf. anpassen, falls dein Linux-Benutzer nicht `pi` heißt)*:
   ```ini
   [Unit]
   Description=MeshCom Communicator
   After=network.target

   [Service]
   User=pi
   WorkingDirectory=/home/pi/meshcom-communicator
   ExecStart=/usr/bin/python3 /home/pi/meshcom-communicator/app.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

3. Speichere die Datei (`Strg+O`, `Enter`, `Strg+X`).

4. Dienst aktivieren und starten:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable meshcom-communicator.service
   sudo systemctl start meshcom-communicator.service
   ```

Du kannst den Status jederzeit mit `sudo systemctl status meshcom-communicator.service` überprüfen.
