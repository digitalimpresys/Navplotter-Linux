# NavPlotter Linux

Nodo NavPlotter fisso per Raspberry Pi (Zero 2 W, Pi 4, Pi 5): stesso protocollo LoRa, mesh, chat, allarmi e
web app dei nodi NavPlotter esistenti, con integrazione Signal K e OpenCPN.

## Cosa serve

- Un Raspberry Pi con Raspberry Pi OS (Bookworm o successivo).
- Un Heltec V2 con il firmware modem (dal webflasher: "Heltec V2 — Raspberry Pi modem"), collegato via USB.
- Una fonte di posizione: Signal K, gpsd, oppure una posizione fissa nelle impostazioni.

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
| `modem` | `/dev/navplotter-modem` | porta seriale dell'Heltec V2 |
| `http_port` | `80` | porta della web app |
| `gps.source` | `auto` | `signalk`, `gpsd`, `fixed` o `auto` (prima Signal K, poi gpsd) |
| `gps.signalk_url` | `http://127.0.0.1:3000` | server Signal K |
| `gps.signalk_token` | vuoto | token se Signal K richiede l'accesso |
| `gps.lat`, `gps.lon` | `0` | posizione per `fixed` |
| `ais.enabled` | `true` | invia le barche NavPlotter come bersagli AIS |
| `ais.destinations` | `["127.0.0.1:10110"]` | indirizzi UDP che ricevono le frasi NMEA |
| `ais.mmsi` | `{}` | MMSI veri per id: `{"A7C94F1B": 247123456}` |
| `id` | vuoto | id del nodo in esadecimale; vuoto prende quello del modem |

## Signal K e OpenCPN

Il nodo invia frasi `!AIVDM` (tipo 18 per la posizione, tipo 24 per nome e tipo) via UDP alla porta 10110.

- **Signal K:** Server → Data Connections → Add: tipo *NMEA 0183*, sorgente *UDP*, porta *10110*.
  Le barche NavPlotter compaiono come navi AIS.
- **OpenCPN:** Opzioni → Connessioni → Aggiungi: *Rete*, protocollo *UDP*, indirizzo `0.0.0.0`, porta *10110*.
  Se OpenCPN gira su un altro computer, aggiungere il suo indirizzo in `ais.destinations`.
- **NMEA 2000:** tramite Signal K con il plugin `signalk-to-nmea2000` (da verificare a bordo).

Senza MMSI configurato, ogni barca riceve un MMSI finto `999xxxxxx` ricavato dall'id: non è un MMSI reale e va
usato solo a bordo.

## App Android

L'API JSON è la stessa dei nodi (`/api/state`, `/api/config`, `/api/messages`...) e risponde anche alle richieste
CORS, quindi l'app può collegarsi all'indirizzo del Raspberry.

## Test

```sh
python3 -m unittest discover tests
```
