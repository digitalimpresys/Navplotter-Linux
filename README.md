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

## Spostare un waypoint e correggere una rotta

Sulla carta, un tocco lungo su un waypoint lo prende: segue il dito e resta dove lo lasci. Nella sua scheda c'è
**Annulla spostamento**. Un tocco lungo sul mare continua ad aggiungere un waypoint nuovo.

Un tocco lungo su un tratto di rotta mette lì un waypoint nuovo, dentro la rotta tra i due estremi del tratto, già
preso col dito: si trascina subito per girare attorno a una secca, un capo o un'isola. Nella sua scheda c'è
**Annulla inserimento**. Vale per le rotte che si vedono sulla carta: quella seguita, quella aperta in
**Rotte > Modifica** (tratteggiata) e quella della pianificazione mostrata sulla carta.

## Uomo in mare (MOB)

Su tutte le pagine della web app c'è un pulsante rosso **MOB**, sopra a tutto il resto. Un tocco mette un waypoint
nella posizione della barca (se il GPS ha appena perso il fix, l'ultima posizione dei due minuti prima), con il
simbolo di un salvagente e il tempo passato da quando è stato premuto; il waypoint diventa la destinazione e la rotta
seguita si ferma. Parte un allarme (due toni ripetuti ogni pochi secondi) su ogni schermo che mostra la web app, e
continua finché il waypoint MOB non viene eliminato nella pagina **Waypoint**.

Il pulsante si sposta come i waypoint: tocco lungo, trascinarlo e lasciarlo dove non dà fastidio. La posizione resta
in quel browser. Un tocco lungo lasciato senza trascinare conta come MOB.

Il modulo è `web/navplotter-mob.js`, lo stesso file dell'app (`www/navplotter-mob.js` in app-NavPlotter).

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

### Telecamera di rete (Tapo C510W e simili)

Al posto della USB si può usare una telecamera WiFi sulla stessa rete del Raspberry, che dà il video in RTSP e si
muove con ONVIF (le Tapo C200, C500, C510W...).

1. Nell'app Tapo: telecamera > Impostazioni > Impostazioni avanzate > **Account telecamera**: crea utente e password.
   La modalità privacy deve essere spenta.
2. Nella pagina **Telecamera** della web app: **Sorgente video** > Telecamera di rete, l'indirizzo IP, l'account e
   la qualità (stream1 alta, stream2 più leggera), poi **Salva**.

Se la telecamera si muove, sul video compaiono quattro frecce: tienine premuta una per girarla. Le impostazioni
restano in `/var/lib/navplotter/camera.json`, leggibile solo dal servizio. Conviene dare alla telecamera un
indirizzo fisso nel router.

Il riconoscimento usa modelli generici (COCO): aiuta a vedere, ma **non sostituisce la guardia**.

## Assistente vocale (facoltativo)

Un assistente che si usa a voce, tutto sul Raspberry e senza internet: il riconoscimento della voce con
[whisper.cpp](https://github.com/ggml-org/whisper.cpp), le risposte dai dati del nodo, la voce con
[Piper](https://github.com/rhasspy/piper) e, se lo installi, un piccolo modello linguistico con
[llama.cpp](https://github.com/ggml-org/llama.cpp) per le domande libere.

Microfono e altoparlante sono quelli del Raspberry: una cuffia Bluetooth o un vivavoce. Si installa con l'utente
del desktop, quello che ha la cuffia (non con sudo):

```
bash /opt/navplotter/tools/voice-install.sh
NAVPLOTTER_LLM=1 bash /opt/navplotter/tools/voice-install.sh     # anche il modello linguistico
```

Scarica circa 700 MB (con il modello linguistico 1,1 GB in più, o 2 GB con `NAVPLOTTER_LLM_MODEL=3b`); se si interrompe, rilancialo e riprende.
Nella web app compare il pulsante del microfono in basso a destra: tocca, parla, tocca di nuovo. Si sente un
segnale quando ascolta. La risposta arriva in cuffia ed è scritta anche sullo schermo. Si possono anche scrivere
le domande.

Domande che capisce: dove siamo, a che velocità andiamo, quanto manca al prossimo waypoint (con l'ora di
arrivo e il resto della rotta), chi c'è vicino, ci sono allarmi, ci sono messaggi, che ore sono, e "manda un
messaggio a tutti ..." o "a <nome> ...". Le altre domande vanno al modello linguistico, se c'è.

Nel pannello dell'assistente (tieni premuto il pulsante del microfono per aprirlo):

- **Sempre in ascolto**: non serve toccare niente. Di' la parola di attivazione (di serie "computer") e la
  domanda, per esempio "computer, quanto manca al waypoint?". Con la parola da sola risponde con un segnale e
  aspetta la domanda. Subito dopo una risposta si può continuare senza ripetere la parola, come in una
  conversazione. Con una cuffia Bluetooth il microfono resta acceso: la cuffia resta nel profilo "vivavoce" e la
  batteria dura meno.
- **Avviso fuori rotta**: se per mezzo minuto la rotta sul fondo (COG del GPS: non c'è una bussola) si scosta
  dalla direzione del waypoint di destinazione più dei gradi impostati (di serie 20), lo dice ("accosta di 25
  gradi a sinistra") e lo ripete ogni 2 minuti; quando si torna in rotta, lo dice. Sotto 1,5 nodi tace.
- **Dialogo libero**: con il modello linguistico installato, le domande lunghe e quelle che non sono comandi
  vanno a lui, che ricorda le ultime battute e comincia a parlare appena ha la prima frase.

- Il servizio è `navplotter-voice` dell'utente: `systemctl --user status navplotter-voice`,
  log con `sudo journalctl _SYSTEMD_USER_UNIT=navplotter-voice.service -f` (Raspberry Pi OS tiene il log degli
  utenti nel giornale di sistema).
- Il modello linguistico non sa il meteo e può sbagliare: per i dati di navigazione valgono le risposte dirette.

## App Android

L'API JSON è la stessa dei nodi (`/api/state`, `/api/config`, `/api/messages`...) e risponde anche alle richieste
CORS, quindi l'app può collegarsi all'indirizzo del Raspberry.

## Test

```sh
python3 -m unittest discover tests
```
