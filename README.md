# NavPlotter Linux

Nodo NavPlotter fisso per Raspberry Pi (Zero 2 W, Pi 4, Pi 5): stesso protocollo LoRa, mesh, chat, allarmi e
web app dei nodi NavPlotter esistenti, con integrazione Signal K e OpenCPN.

## Cosa serve

- Un Raspberry Pi con Raspberry Pi OS (Bookworm o successivo).
- Un Heltec V2 o V4 con il firmware modem (`firmware/heltec-modem`), collegato via USB. La V4 con il suo modulo
  GPS fa anche da GPS: non serve altro.
- Facoltativo: un GPS USB (per esempio G-Mouse), oppure una posizione fissa, gpsd o Signal K.

## Installazione

Con un comando solo:

```sh
curl -sSL https://raw.githubusercontent.com/digitalimpresys/Navplotter-Linux/main/install.sh | sudo bash
```

Oppure da una copia del repository: `sudo ./install.sh`. Per un altro ramo: `NAVPLOTTER_BRANCH=<ramo>`.

L'installer mette il programma in `/opt/navplotter`, crea l'utente `navplotter`, il link `/dev/navplotter-modem`
per l'Heltec V2 (CP2102, 10c4:ea60) e il servizio `navplotter`. La web app è su `http://<raspberry>/`.

Comandi utili:

```sh
sudo systemctl status navplotter
sudo journalctl -u navplotter -f
sudo systemctl restart navplotter
```

## Impostazioni

Sono in `/var/lib/navplotter/settings.json`. Nome, tipo, allarmi e lingua si cambiano dalla web app; il resto
a mano, poi `sudo systemctl restart navplotter`.

| Chiave | Default | Note |
|---|---|---|
| `modem` | `/dev/navplotter-modem` | porta seriale dell'Heltec; se non c'è, la prima Heltec V2 o V4 collegata |
| `http_port` | `80` | porta della web app |
| `gps.source` | `serial` | `serial` (GPS USB o seriale NMEA, per esempio G-Mouse), `none` (nessuna posizione propria: il nodo riceve, inoltra e mostra gli altri), `fixed`, `gpsd`, `signalk` o `auto` (prima Signal K, poi gpsd) |
| `gps.serial_port` | `auto` | porta del GPS; `auto` prende il primo dispositivo seriale USB che non è il modem, e senza GPS USB il GPS della Heltec V4 (`gps.source` `modem` usa solo quello) |
| `gps.serial_baud` | `9600` | velocità del GPS seriale (molti vecchi GPS usano 4800) |
| `gps.signalk_url` | `http://127.0.0.1:3000` | server Signal K |
| `gps.signalk_token` | vuoto | token se Signal K richiede l'accesso |
| `gps.lat`, `gps.lon` | `0` | posizione per `fixed` |
| `ais.enabled` | `false` | invia le barche NavPlotter come bersagli AIS |
| `ais.destinations` | `["127.0.0.1:10110"]` | indirizzi UDP che ricevono le frasi NMEA |
| `ais.mmsi` | `{}` | MMSI veri per id: `{"A7C94F1B": 247123456}` |
| `id` | vuoto | id del nodo in esadecimale; vuoto prende quello del modem |

## Signal K e OpenCPN (facoltativo)

Spento di default: per usarlo mettere `"ais": {"enabled": true}` e una fonte di posizione.

Il nodo invia frasi `!AIVDM` (tipo 18 per la posizione, tipo 24 per nome e tipo) via UDP alla porta 10110.

- **Signal K:** Server → Data Connections → Add: tipo *NMEA 0183*, sorgente *UDP*, porta *10110*.
  Le barche NavPlotter compaiono come navi AIS.
- **OpenCPN:** Opzioni → Connessioni → Aggiungi: *Rete*, protocollo *UDP*, indirizzo `0.0.0.0`, porta *10110*.
  Se OpenCPN gira su un altro computer, aggiungere il suo indirizzo in `ais.destinations`.
- **NMEA 2000:** tramite Signal K con il plugin `signalk-to-nmea2000` (da verificare a bordo).

Senza MMSI configurato, ogni barca riceve un MMSI finto `999xxxxxx` ricavato dall'id: non è un MMSI reale e va
usato solo a bordo.

## Meteo e pianificazione

Come nell'app Android, la web app ha vento, corrente e temporali sopra la carta (pulsante ≋) e, nella voce
**Navigazione**, la scheda **Pianifica**: orari, vento, corrente e onda per ogni tratta di una rotta, a motore o a
vela con la polare della barca. A vela i bordi vengono dalle isocrone, la strada più veloce con vento e corrente
previsti (sulla carta si vede un'isocrona per ogni ora), oppure dalle regole semplici sulle lay-line. Le isocrone
non conoscono la terra: la rotta va tracciata con i waypoint al largo di coste e isole. Le previsioni sono di Open-Meteo: le scarica il Raspberry (`/meteo`) quando ha
internet, altrimenti il browser se ce l'ha lui, e restano nel browser per l'uso senza rete. Il modulo è
`web/navplotter-meteo.js`, lo stesso file dell'app (`www/navplotter-meteo.js` in app-NavPlotter).

## Spostare un waypoint

Sulla carta, un tocco lungo su un waypoint lo prende: segue il dito e resta dove lo lasci. Nella sua scheda c'è
**Annulla spostamento**. Un tocco lungo sul mare continua ad aggiungere un waypoint nuovo.

## Telecamera e riconoscimento oggetti (facoltativo)

Con una telecamera USB, la web app ha una voce **Telecamera** con il video dal vivo, una vista notturna (più
luminosa e con più contrasto) e, con l'AI HAT Hailo, i riquadri degli oggetti riconosciuti: barche e persone in
rosso, gli altri oggetti in giallo.

- Il servizio `navplotter-camera` viene installato da solo se c'è una telecamera USB o un Hailo; altrimenti
  `NAVPLOTTER_CAMERA=1` prima del comando di installazione.
- Per il riconoscimento serve il software Hailo di Raspberry Pi OS: `sudo apt install hailo-all`. Il modello è
  scelto da solo tra quelli in `/usr/share/hailo-models` (per l'AI HAT da 26 TOPS `yolov8s_h8.hef`).
- Log: `journalctl -u navplotter-camera -f`.
- **Registra** salva un fotogramma al secondo, da scaricare come zip: serve per addestrare un modello proprio.
  Un modello proprio (`.hef` più `.json` con le classi) va in `/var/lib/navplotter/models` e ha la precedenza su
  quelli di sistema. Come addestrarlo: [training/README.md](training/README.md).

Il riconoscimento usa modelli generici (COCO): aiuta a vedere, ma **non sostituisce la guardia**.

## App Android

L'API JSON è la stessa dei nodi (`/api/state`, `/api/config`, `/api/messages`...) e risponde anche alle richieste
CORS, quindi l'app può collegarsi all'indirizzo del Raspberry.

## Test

```sh
python3 -m unittest discover tests
```
