# Protocollo NavPlotter

Specifica del protocollo radio LoRa, della mesh, della chat, dell'allarme di collisione, del tracciato e
dell'API JSON dei nodi NavPlotter, ricavata dal codice dei firmware esistenti. Serve come riferimento comune
per il nodo Linux (questo repository) e per l'app Android.

Dove questo documento e il codice non coincidono, vale il codice. Le differenze tra un firmware e l'altro
sono raccolte nella sezione [Differenze tra i firmware](#9-differenze-tra-i-firmware).

**Fonti** (commit letti il 15/10/2026):

| Repository | Commit | Cosa contiene |
|---|---|---|
| `Navplotter-tdeck-plus` | `97239a9` | plotter T-Deck Plus (`src/`), nodo Heltec V3/V2 (`navplotter-heltec/`), simulatori Pippa e Serpico (`simulatori/`) |
| `Navplotter-esp32-32e` | `f135d69` | plotter ESP32-32E "Boat", radio RFM95W (SX1276) |
| `Navplotter-Pico-W` | `e123a3a` | nodo Raspberry Pi Pico W / Pico 2 W con SX1262 |
| `Navplotter-Segnalatore` | `3265430` | boa (Heltec V3/V2), solo posizioni |
| `Navplotter-Esp32-S3-Touch-LCD-4` | `6a6eb88` | schermo che fa da client dell'API JSON, senza radio |
| `app-NavPlotter` | `ee4f355` | per ora solo il README: la web app dei nodi impacchettata con Capacitor |

I file condivisi sono identici byte per byte in tutti i repository che li hanno (verificato con md5):
`traffic_packet.h` (pacchetto posizione, radio, mesh), `nav_messages.h` (chat), `nav_quiet.h` (nodi
silenziati), `nav_track.h` (tracciato). Anche `web/index.html` è lo stesso file in T-Deck, nodo Heltec,
ESP32-32E e Pico W; il Segnalatore ne ha una versione propria.

---

## 1. Radio

| Parametro | Valore | Sorgente |
|---|---|---|
| Frequenza | 869,525 MHz | `LORA_FREQ` |
| Larghezza di banda | 125 kHz | `LORA_BW` |
| Spreading factor | SF9 | `LORA_SF` |
| Coding rate | 4/5 | `LORA_CR 5` |
| Sync word | `0x4E` (privata, non LoRaWAN) | `LORA_SYNC` |
| Preambolo | 8 simboli | `LORA_PREAMBLE` |
| Header | esplicito (default RadioLib) | |
| CRC | attivo, 16 bit (`setCRC(2)` su SX126x, `setCRC(true)` su SX127x) | |
| Low data rate optimize | no (simbolo da 4,1 ms) | automatico in RadioLib |
| Potenza | 22 dBm su SX1262 (Heltec V3, T-Deck, Pico W), 20 dBm su SX1276 (Heltec V2, RFM95W) | `LORA_POWER_DBM` |
| Limite di corrente PA | 140 mA (`setCurrentLimit(140)`) | |
| TCXO | 1,8 V su DIO3 per SX1262 (Heltec V3, T-Deck); il Pico W prova 1,8 V e poi senza TCXO | |
| Libreria | RadioLib ^6.6.0 | `platformio.ini` |

Il sync word a un byte `0x4E` viene tradotto da RadioLib nel formato a due byte dell'SX126x, e le radio
SX1262 e SX1276 si sentono tra loro (il README del nodo Heltec lo conferma per la V2).

**Banda e duty cycle.** 869,4–869,65 MHz è la sottobanda europea con 500 mW ERP e duty cycle del 10%.
Nessun firmware conta il tempo di trasmissione né ascolta il canale prima di trasmettere (niente CAD/LBT):
l'unica protezione dalle collisioni è il ritardo casuale.

**Tempo in aria** (SF9, BW125, CR4/5, preambolo 8, header esplicito, CRC):

| Pacchetto | Byte | Tempo in aria |
|---|---|---|
| Posizione | 34 | circa 247 ms |
| Conferma chat | 28 | circa 226 ms |
| Messaggio chat pieno (63 caratteri) | 91 | circa 513 ms |

Un nodo in movimento trasmette la posizione ogni 10–12 s, cioè circa il 2% del tempo; ogni pacchetto di un
vicino può essere ritrasmesso fino a due volte nella rete (vedi mesh), quindi con molte barche vicine il 10%
si avvicina. Il nodo Linux dovrebbe tenere il conto del proprio tempo di trasmissione.

## 2. Pacchetto posizione (`'N','P'`)

Struttura `TrafficPacket` in `traffic_packet.h`: `packed`, interi **little-endian** (ESP32, RP2040 e i
Raspberry Pi sono tutti little-endian, ma il nodo Linux deve dichiararlo, per esempio `struct` con `<`).
Lunghezza fissa **34 byte**.

| Offset | Tipo | Campo | Significato |
|---|---|---|---|
| 0 | `char[2]` | `magic` | `'N','P'` (0x4E 0x50) |
| 2 | `uint8` | `version` | 1 |
| 3 | `uint8` | `ttl` | ritrasmissioni ancora permesse; parte da `MESH_TTL` = 2 |
| 4 | `uint32` | `id` | identificativo del nodo di origine |
| 8 | `uint8` | `seq` | numero progressivo del pacchetto (mod 256) |
| 9 | `uint8` | `flags` | vedi sotto |
| 10 | `int32` | `lat` | gradi × 10⁷ |
| 14 | `int32` | `lon` | gradi × 10⁷ |
| 18 | `uint16` | `cog` | rotta in decimi di grado (0–3599) |
| 20 | `uint16` | `sog` | velocità in centesimi di nodo |
| 22 | `char[12]` | `name` | nome, 11 caratteri al massimo + terminatore, resto a zero |

**`flags`:**

| Bit | Maschera | Significato |
|---|---|---|
| 0 | `0x01` | barca simulata (`TRAFFIC_FLAG_SIMULATED`, solo i simulatori) |
| 1–2 | `0x06` | tipo di nodo: 0 barca, 1 persona (uomo a mare), 2 oggetto (boa, tender); 3 non usato, chi riceve lo tratta come barca |
| 3–5 | `0x38` | classe di sonno: indice in `{0, 1, 2, 5, 10, 15, 30, 60}` minuti; 0 = nodo sempre acceso |
| 6–7 | | non usati, a zero |

**Identificativo del nodo.** Sui nodi ESP32 è `(uint32)(mac >> 16) ^ (uint32)mac`, con `mac` =
`ESP.getEfuseMac()`. Sul Pico W è un FNV-1a a 32 bit sull'ID unico della flash. Scritto in JSON come 8 cifre
esadecimali maiuscole (`"A7C94F1B"`). Nessuno controlla che sia unico: due nodi con lo stesso id si
confondono.

**Quando si trasmette** (nodo Heltec, Pico W, ESP32-32E, Segnalatore):

- la prima volta 5–8 s dopo l'accensione;
- poi ogni 10 s se la barca si muove (velocità GPS ≥ 0,5 nodi), ogni 30 s se è ferma, più 0–2 s casuali;
- solo con un fix GPS valido e recente (meno di 5 s), e solo se l'opzione "condividi la mia posizione"
  (`share`) è attiva; se è spenta il nodo è invisibile ma riceve, dà l'allarme e ritrasmette gli altri;
- `cog` è l'ultima rotta GPS valida presa in movimento, tenuta ferma quando la barca si ferma (da fermi la
  rotta GPS è rumore); `sog` è 0 se la barca non si muove;
- `seq` aumenta di uno a ogni posizione propria. Le boe che dormono lo conservano nella RTC memory.

**Ricezione.** Un pacchetto è una posizione se è lungo esattamente 34 byte e il byte 1 è `'P'`; poi si
controllano `magic`, `version == 1` e `id` diverso dal proprio. I campi ricevuti:

- `name` viene troncato a 11 caratteri e ripulito: caratteri fuori da 0x20–0x7E e `" \ < > &` diventano `?`;
- tipo fuori intervallo → barca;
- `relayed` = `ttl < MESH_TTL` sulla copia arrivata per prima;
- l'RSSI salvato è quello della copia arrivata per prima.

**Bersagli vecchi e persi.** Un bersaglio è "vecchio" (stale) dopo 60 s senza pacchetti e viene tolto dopo
180 s. Per un nodo con classe di sonno di *m* minuti: vecchio dopo *m* × 90 s + 30 s, tolto dopo
*m* × 180 s + 60 s. Al massimo 30 bersagli (20 su T-Deck ed ESP32-32E); quando la tabella è piena i nuovi
vengono ignorati.

## 3. Mesh

Ogni nodo ritrasmette i pacchetti degli altri finché hanno `ttl > 0`. Il pacchetto ritrasmesso è identico
all'originale (stessi `id`, `seq`, nome, posizione) con `ttl` diminuito di uno. Con `MESH_TTL` = 2 un
pacchetto fa al massimo tre salti: l'originale e due ritrasmissioni.

**Copie già viste** (`meshAlreadySeen`): una tabella di 24 righe, una per `id`, con l'ultimo `seq` e l'ora.
Un pacchetto è già visto se la riga di quell'`id` ha meno di 60 s e `(uint8)(seq_salvato − seq) < 16`, cioè
`seq` è uguale all'ultimo o uno dei 15 precedenti. Altrimenti la riga viene aggiornata con il nuovo `seq`
(se manca, prende un posto libero o quello più vecchio).

**Posizioni:**

1. Arriva una posizione già vista: si cancella la propria ritrasmissione in coda dello stesso `id`+`seq`
   (qualcun altro l'ha già fatta) e la si scarta.
2. Arriva una posizione nuova: la si usa e, se `ttl > 0`, la si mette in coda per la ritrasmissione dopo un
   ritardo casuale di 300–1500 ms, con `ttl − 1`. La coda ha 4 posti; se è piena il pacchetto non viene
   ritrasmesso.

La ritrasmissione non dipende dall'opzione `share`.

**Messaggi chat:** stessa tabella, ma con chiave `id XOR 0x4D534721` (`MSG_DEDUP_MARK`) così che i numeri
dei messaggi non si mescolino con quelli delle posizioni. Si ritrasmette un pacchetto chat non ancora visto,
con `ttl > 0`, che non sia indirizzato solo a noi (quindi anche i messaggi "a tutti"), dopo 300–1500 ms, in
una coda di 3 posti. Per la chat non c'è la cancellazione quando si sente la copia di un altro.

**Ordine delle trasmissioni** nel ciclo principale, una alla volta, mai mentre si sta già trasmettendo:

1. la propria posizione, se è il momento;
2. un pacchetto chat: prima le conferme, poi i propri messaggi da inviare o ripetere, poi le ritrasmissioni
   di messaggi altrui;
3. una ritrasmissione di posizione.

Una trasmissione che non finisce entro 3 s viene interrotta e la radio torna in ricezione.

## 4. Chat (`'N','M'`)

Struttura `MessagePacket` in `nav_messages.h`, `packed`, little-endian. Lunghezza **variabile**:
27 byte di intestazione più il testo con il suo terminatore, da 28 a 91 byte.

| Offset | Tipo | Campo | Significato |
|---|---|---|---|
| 0 | `char[2]` | `magic` | `'N','M'` (0x4E 0x4D) |
| 2 | `uint8` | `version` | 1 |
| 3 | `uint8` | `ttl` | come per le posizioni, parte da 2 |
| 4 | `uint32` | `id` | mittente |
| 8 | `uint8` | `seq` | numero di questa trasmissione, per la mesh; contatore proprio della chat, parte da un valore casuale |
| 9 | `uint8` | `kind` | 0 testo, 1 ricevuto, 2 letto |
| 10 | `uint32` | `to` | destinatario, 0 = tutti |
| 14 | `uint8` | `ref` | testo: numero del messaggio; conferme: numero del messaggio confermato |
| 15 | `char[12]` | `name` | nome del mittente |
| 27 | `char[≤64]` | `text` | testo + terminatore; si trasmette solo la parte usata |

Numeri dei messaggi (`ref`) e `seq` partono da valori casuali all'accensione. Il testo è al massimo di 63
caratteri ASCII stampabili (0x20–0x7E); gli altri diventano `?`, sia in partenza che in arrivo. Le conferme
hanno testo vuoto: 28 byte.

**Ricezione**: si accetta un pacchetto lungo 28–91 byte con `magic` `'N','M'`, `version` 1 e mittente
diverso da sé.

**Messaggio a un nodo** (`to` = id del destinatario):

- il mittente lo trasmette fino a 4 volte, ogni 7–8,5 s, finché non arriva una conferma; dopo 4 tentativi
  lo stato diventa "nessuna risposta";
- il destinatario, alla prima copia, lo salva come non letto e risponde `kind` 1 (ricevuto) dopo
  200–600 ms;
- se arriva di nuovo lo stesso messaggio (stesso mittente e numero) con una trasmissione nuova, vuol dire
  che la conferma si è persa: si rimanda "ricevuto", oppure "letto" se è già stato letto;
- quando qualcuno lo legge (sullo schermo o con `/api/messages/read`), il destinatario manda `kind` 2
  (letto) due volte, dopo 100 ms e dopo 4 s, perché questa conferma non viene ripetuta su richiesta;
- il mittente passa a "consegnato" con la conferma 1 e a "letto" con la 2 (da "letto" non si torna
  indietro).

**Messaggio a tutti** (`to` = 0): trasmesso due volte a 7–8,5 s di distanza, nessuna conferma; stato
"inviato".

Ogni nodo tiene in memoria gli ultimi 12 messaggi (propri e ricevuti); si perdono allo spegnimento.

**Stati** (campo `status` nell'API):

| Valore | Nome | Significato |
|---|---|---|
| 0 | `MSG_INCOMING` | ricevuto da un altro |
| 1 | `MSG_PENDING` | nostro, in invio |
| 2 | `MSG_SENT` | nostro, a tutti, inviato |
| 3 | `MSG_DELIVERED` | nostro, arrivato all'altro nodo |
| 4 | `MSG_READ` | nostro, letto |
| 5 | `MSG_NO_ANSWER` | nostro, mai confermato |

## 5. Allarme di collisione (CPA/TCPA)

Calcolato una volta al secondo per ogni bersaglio, su un piano locale in metri centrato sulla propria
posizione:

```
x (est)   = (lon_b − lon_noi) × 111320 × cos(lat_noi)
y (nord)  = (lat_b − lat_noi) × 110540
```

1. Senza fix GPS proprio non si calcola nulla.
2. Se il bersaglio non è vecchio, la sua posizione viene portata avanti con la sua rotta e velocità per il
   tempo passato dall'ultimo pacchetto. Distanza e rilevamento si calcolano comunque, anche per i bersagli
   vecchi; CPA e allarme no.
3. Velocità propria: quella GPS se ≥ 0,5 nodi, altrimenti 0; rotta propria come nel pacchetto.
4. Velocità relativa `v = v_bersaglio − v_noi`. Se `|v|² < 0,06` (m/s)², circa mezzo nodo, nessun
   avvicinamento.
5. `TCPA = −(r · v) / |v|²`; se è negativo ci si sta allontanando.
6. `CPA = |r + v × TCPA|`, in miglia (1852 m).
7. Allarme se: distanza attuale entro il raggio d'allarme (3, 6 o 12 NM; 0 = nessun limite),
   `CPA ≤ distanza di passaggio` e `TCPA ≤ tempo di preavviso`, e il nodo non è silenziato.

**Profili** (`alarmProfile`):

| Indice | Uso | Distanza di passaggio | Preavviso |
|---|---|---|---|
| 0 | porto e acque ristrette | 0,05 NM | 2 min |
| 1 (default) | costiero | 0,1 NM | 5 min |
| 2 | mare aperto | 0,5 NM | 10 min |
| 3 | notte o scarsa visibilità | 1,0 NM | 20 min |
| 4 | personalizzato | `alarmCpa` 0,02–5 NM | `alarmTcpa` 1–60 min |

Il bersaglio in allarme con il TCPA più breve dà il nome all'allarme (`alarmName`).

**Nodi silenziati** (`nav_quiet.h`): una lista di al massimo 16 id (amici che navigano insieme) che non fanno
mai scattare l'allarme ma si vedono e ricevono messaggi. Dura fino allo spegnimento: alla riaccensione ogni
allarme è di nuovo attivo. Se la lista è piena il più vecchio lascia il posto.

**Silenzio**: il pulsante della web app ferma il cicalino del nodo per un minuto (`/api/mute`); un allarme
nuovo suona sempre.

## 6. Waypoint e rotte

Non viaggiano via LoRa: restano sul nodo e la web app li legge e scrive con `/api/userdata`. Formato:

```json
{
  "wps": [{"id": 1, "name": "Faro", "lat": 45.4637, "lon": 12.1471}],
  "dest": 1,
  "nextId": 2,
  "routes": [{"id": 1, "name": "Rotta 1", "wps": [1, 3, 2]}],
  "route": 1,
  "nextRouteId": 2,
  "version": 7
}
```

- `dest`: id del waypoint di destinazione, o `null`; `route`: id della rotta seguita, o `null`;
- seguendo una rotta, la web app passa al waypoint successivo quando si arriva entro 0,05 NM;
- nomi al massimo di 20 caratteri;
- `version` c'è solo sul T-Deck, che ha uno schermo suo e può cambiare i waypoint da sé: la web app vede
  `udVersion` in `/api/state` e ricarica quando cambia;
- il nodo Heltec e gli altri senza schermo salvano il documento così com'è (al massimo 32 KB, deve iniziare
  con `{`) senza guardarne il contenuto.

## 7. Tracciato e GPX

`nav_track.h`. Il nodo registra un punto ogni 25 m percorsi, nessuno da fermo, solo con HDOP < 5. Al massimo
4000 punti (circa 54 NM); quando è pieno toglie il quarto più vecchio. Salvataggio su flash ogni 2 minuti o
ogni 20 punti, nel file `/track.bin`:

```
"NTRK" | uint32 trackId | uint32 punti tolti | punti { int32 lat×1e7, int32 lon×1e7, uint32 ora UTC (s dal 1970, 0 = ignota) }
```

Per la web app:

- `/api/state` → `"track": {"id": 3, "n": 1520, "first": 1000}`: `n` = punti totali compresi quelli tolti,
  `first` = indice del primo ancora presente;
- `GET /api/track?from=N` → `{"id":3,"first":1000,"from":1200,"points":[[lat×1e6,lon×1e6,ora],...]}`,
  **attenzione: qui le coordinate sono × 10⁶**, non × 10⁷;
- `GET /api/track.gpx` → GPX 1.1, `creator="NavPlotter"`, un `trk` con il nome del nodo, coordinate con 6
  decimali e `<time>` ISO 8601 quando l'ora è nota; file `navplotter-track-<id>.gpx`;
- `POST /api/track/new` → nuovo tracciato, `trackId` + 1.

## 8. API JSON dei nodi

Server HTTP sulla porta 80. Il nodo crea la sua rete WiFi `NavPlotter-<nome>` (spazi → `_`, password di
default `navplotter`) ed è raggiungibile a `http://192.168.4.1`. Il nodo Heltec e il Pico W possono anche
entrare nel WiFi di bordo e si annunciano in mDNS come `<nome>.local` (nome in minuscolo, caratteri non
alfanumerici → `-`, servizio `_http._tcp`).

Le risposte JSON hanno `Cache-Control: no-store`. **Nessun firmware manda intestazioni CORS**: la web app
funziona perché è servita dal nodo stesso; l'app Android con Capacitor dovrà usare richieste native
(per esempio `CapacitorHttp`) oppure il nodo Linux dovrà aggiungere `Access-Control-Allow-Origin`.

Le richieste POST hanno i parametri in `application/x-www-form-urlencoded` (`URLSearchParams`), tranne
`/api/userdata` che riceve il JSON come corpo (`Content-Type: text/plain`). La web app interroga
`/api/state` una volta al secondo.

| Metodo e percorso | Cosa fa | Chi ce l'ha |
|---|---|---|
| `GET /` | la web app, `index.html` compresso gzip | tutti |
| `GET /api/state` | stato completo | tutti |
| `GET /api/config` | impostazioni | tutti |
| `POST /api/config` | cambia le impostazioni | tutti |
| `GET`/`POST /api/userdata` | waypoint e rotte | tutti |
| `GET /api/messages` | elenco messaggi | tutti tranne il Segnalatore |
| `POST /api/messages` | invia: `to` (id esadecimale, `0` = tutti), `name` (nome del destinatario se non è tra i bersagli), `text` (1–63 caratteri) | tutti tranne il Segnalatore |
| `POST /api/messages/read` | segna letta una conversazione: `peer` (id) oppure `all` | tutti tranne il Segnalatore |
| `POST /api/quiet` | `id`, `on` = 1/0: silenzia un nodo | tutti tranne il Segnalatore |
| `POST /api/mute` | `on` = 1/0: silenzia il cicalino per 60 s | nodo Heltec, Segnalatore |
| `GET /api/track`, `GET /api/track.gpx`, `POST /api/track/new` | tracciato | T-Deck, nodo Heltec |
| `GET /tiles/z/x/y.jpg` | carte dalla scheda SD | chi ha la SD |

Errori: `400 {"ok":false}`; risposta di successo `{"ok":true}`.

### `GET /api/state`

```json
{
  "own": {"fix": true, "lat": 45.4637000, "lon": 12.1471000, "sog": 5.20, "cog": 123.4,
          "cogValid": true, "moving": true, "sats": 9, "hdop": 0.9, "time": "09:12:33"},
  "targets": [
    {"id": "A7C94F1B", "name": "Pippa", "type": 0, "sim": true,
     "lat": 45.4700000, "lon": 12.1500000, "sog": 4.00, "cog": 270.0, "age": 3,
     "rssi": -87, "relayed": false, "stale": false,
     "dist": 0.742, "brg": 18, "cpaValid": true, "cpa": 0.080, "tcpa": 245, "alarm": true}
  ],
  "alarm": true, "alarmName": "Pippa",
  "lora": {"ok": true, "tx": 120, "rx": 340, "relay": 55, "freq": 869.525},
  "gpsBaud": 9600, "sd": false, "battery": 3.98, "uptime": 3600,
  "sta": false, "staIp": "", "apIp": "192.168.4.1",
  "msgStamp": 2873461, "quiet": ["BA2680C5"],
  "track": {"id": 1, "n": 250, "first": 0}
}
```

- `own.time` è l'ora UTC del GPS; `hdop` 99.9 se ignoto; `cogValid` = una rotta valida è stata vista;
- per i bersagli: `age` in secondi, `dist` e `cpa` in NM, `brg` in gradi veri, `tcpa` in secondi, `sog` in
  nodi;
- `battery` in volt, 0 dove non si misura (la web app mostra "-");
- `msgStamp` cambia quando cambia qualcosa nei messaggi: la web app chiede `/api/messages` solo allora;
- `udVersion` c'è solo sul T-Deck; `track` solo dove c'è il tracciato;
- `tx` conta le posizioni proprie, `relay` le ritrasmissioni e tutti i pacchetti chat.

### `GET /api/config`

```json
{"name": "SERPICO", "type": 0, "share": true, "alarmRange": 3, "lang": "it", "tiles": 0, "sound": true,
 "id": "A7C94F1B", "version": "0.9.0", "wifiOff": 0, "sleep": 0, "msg": true,
 "alarmProfile": 1, "alarmCpa": 0.10, "alarmTcpa": 5,
 "net": 1, "staSsid": "", "staPassSet": false, "ssid": "NavPlotter-SERPICO", "host": "serpico.local"}
```

- `tiles`: 0 online (OpenStreetMap), 1 scheda SD, 2 nessuna;
- `net`: 0 off-grid, 1 WiFi del nodo con carte online, 2 WiFi di bordo;
- `msg: true` fa comparire la chat nella web app;
- `apOnly: true` (T-Deck, ESP32-32E) nasconde la scelta della rete;
- la password del WiFi di bordo non viene mai restituita, solo `staPassSet`;
- `wifiOff` (0, 5, 10, 30 min) e `sleep` (0–7) valgono solo per gli oggetti (boe).

### `POST /api/config`

Parametri, tutti facoltativi: `name`, `type` (0–2), `share` (`1`/`0`), `alarmRange` (0, 3, 6, 12),
`alarmProfile` (0–4), `alarmCpa` (0,02–5), `alarmTcpa` (1–60), `lang` (`it`/`en`), `tiles` (0–2), `sound`
(`1`/`0`), `wifiPass` (8–32 caratteri), `net` (0–2), `staSsid` (≤ 32), `staPass` (≤ 64, vuoto = tiene
quella salvata), `wifiOff`, `sleep`. Valori fuori intervallo vengono ignorati. La risposta è la
configurazione con in più `"restart": true|false`; se cambia la rete il nodo si riavvia dopo 500 ms.

### `GET /api/messages`

```json
{"stamp": 2873461, "messages": [
  {"mine": false, "all": false, "peer": "A7C94F1B", "status": 0, "unread": true, "age": 42,
   "name": "Pippa", "text": "Ci vediamo al faro"}
]}
```

`peer` è l'altro nodo (per i propri messaggi a tutti `"00000000"`), `name` il suo nome, `age` in secondi.

## 9. Differenze tra i firmware

| # | Dove | Differenza | Conseguenza |
|---|---|---|---|
| 1 | T-Deck | `sendPosition()` non scrive `flags`: trasmette sempre tipo barca, senza classe di sonno; `/api/config` ha `"type":0` fisso | il T-Deck non può presentarsi come persona o oggetto |
| 2 | T-Deck | la frequenza si cambia nel file di configurazione (`loraFreq=`, 863–870 MHz); gli altri hanno 869,525 fissa | un T-Deck con frequenza cambiata non sente più gli altri |
| 3 | T-Deck | trasmette con `gps.location.isValid()` senza controllare che il fix sia recente; gli altri vogliono un fix di meno di 5 s | dopo una perdita del GPS il T-Deck continua a mandare l'ultima posizione |
| 4 | T-Deck | in `/api/state` distanza e rilevamento sono calcolati sulla sfera (TinyGPS) senza portare avanti la posizione, mentre il CPA usa il piano locale; `sim` sempre `false` | piccole differenze nei numeri mostrati |
| 5 | T-Deck | `/api/userdata` è ricostruito dalle strutture del plotter, con `version`; `udVersion` in `/api/state` | gli altri salvano il JSON così com'è e non hanno `version` |
| 6 | T-Deck, ESP32-32E | al massimo 20 bersagli; gli altri 30 | |
| 7 | ESP32-32E, Pico W | niente `/api/track*` né `/api/mute` | la web app nasconde il tracciato se `track` manca in `/api/state` |
| 8 | ESP32-32E | il pin di interrupt della radio non è collegato: lo stato della radio viene letto a ripetizione (polling); la radio divide il bus SPI con la SD | nessuna differenza in aria |
| 9 | Pico W | id del nodo da FNV-1a sull'ID della flash, non dal MAC | stesso formato, diverso calcolo |
| 10 | Segnalatore | accetta solo pacchetti lunghi 34 byte: niente chat, né ricezione né ritrasmissione; ritrasmette le posizioni | una boa non fa da ponte per i messaggi |
| 11 | Simulatori Pippa e Serpico | solo posizioni, `flags` = simulato, ogni 10–12 s; ritrasmettono le posizioni altrui; hanno una modalità di prova che ignora i pacchetti diretti per verificare la mesh | i nodi mostrano `sim: true` |
| 12 | SX1262 / SX1276 | `setCRC(2)` contro `setCRC(true)`, TCXO solo sugli SX1262 | entrambi CRC a 16 bit: compatibili |
| 13 | tutti | `version` in `/api/config`: `"0.9.0"` Heltec, `"0.4.1"` Pico W, `"0.7.0"` ESP32-32E e Segnalatore, `"T-Deck"` sul T-Deck | non c'è una versione di protocollo nell'API; il client deve tollerare campi mancanti |
| 14 | tutti | nessun limite al duty cycle, nessun ascolto prima di trasmettere | da considerare con molti nodi vicini |

## 10. La Heltec V2 come modem USB

Decisione presa: il primo nodo Linux usa una **Heltec WiFi LoRa 32 V2** collegata via USB a un
**Raspberry Pi 4 con Raspberry Pi OS**. Il firmware attuale del nodo Heltec non basta: sulla seriale stampa
solo righe leggibili (`RX Pippa 45.46 ...`) e comandi di prova, non permette di trasmettere pacchetti né
riporta la chat. Serve un firmware "modem" piccolo, da tenere in questo repository (per esempio
`firmware/heltec-modem/`), che riusi `traffic_packet.h` per i parametri radio.

**Divisione del lavoro (proposta):** il modem è solo una radio. Mesh, deduplicazione, chat, allarmi,
bersagli, API e web app stanno sul Raspberry. Così la logica esiste una volta sola sul nodo Linux, si
aggiorna senza riflashare la scheda e la stessa logica servirà anche con una HAT SX1262 a SPI.

**Cosa deve fare il firmware modem:**

- radio SX1276 con i parametri della sezione 1 (pin della V2: NSS 18, SCK 5, MOSI 27, MISO 19, RST 14,
  DIO0 26, DIO1 35), 20 dBm, CRC attivo, sempre in ricezione tra una trasmissione e l'altra;
- consegnare al Raspberry ogni pacchetto ricevuto con CRC buono, con RSSI e SNR, senza filtrarlo;
- trasmettere i byte che il Raspberry manda, così come sono, e dire quando ha finito;
- una trasmissione alla volta: rispondere "occupato" invece di mettere in coda;
- tenere il conto del tempo in aria dell'ultima ora e rifiutare le trasmissioni oltre il 10% (la radio è
  sua, è il posto più sicuro per il limite di legge);
- comunicare il proprio id calcolato dal MAC con la stessa formula degli altri nodi ESP32, che il nodo
  Linux userà come proprio id salvo diversa impostazione;
- display OLED facoltativo (sulla V2 provata non rispondeva): il modem deve funzionare senza.

**Collegamento seriale (proposta):** USB a 115200 baud 8N1, righe di testo terminate da `\n`, pacchetti in
esadecimale. Un pacchetto pieno sono 182 caratteri: a 115200 baud ci vogliono circa 16 ms, molto meno del
tempo in aria. Le righe si leggono anche a mano con un terminale.

| Direzione | Riga | Significato |
|---|---|---|
| modem → Pi | `READY <versione> <id esadecimale> <MHz> <SF> <dBm>` | all'avvio e in risposta a `INFO` |
| modem → Pi | `RX <esadecimale> <rssi> <snr>` | pacchetto ricevuto |
| modem → Pi | `TXOK` / `TXERR <codice>` / `BUSY` / `DUTY` | esito di una trasmissione |
| modem → Pi | `# <testo>` | messaggio diagnostico, da ignorare |
| Pi → modem | `TX <esadecimale>` | trasmetti questi byte (1–255) |
| Pi → modem | `INFO` | ripeti `READY` |

**Sul Raspberry:**

- la V2 ha un convertitore USB-seriale CP2102 (VID:PID `10c4:ea60`), che compare come `/dev/ttyUSB0`.
  Una regola udev può dargli un nome fisso, per esempio `/dev/navplotter-modem`; l'utente del servizio deve
  essere nel gruppo `dialout`;
- aprendo la porta, le linee DTR/RTS resettano l'ESP32 (è il circuito di caricamento automatico): il nodo
  Linux deve aspettare `READY` dopo ogni apertura e trattare ogni `READY` inatteso come un riavvio del
  modem;
- i tempi casuali della mesh (300–1500 ms) e delle conferme (100–600 ms) si contano sul Raspberry: la
  latenza della seriale è trascurabile.

**Alternativa senza firmware nuovo, solo per prove:** con il firmware attuale del nodo Heltec, il Raspberry
può collegarsi al WiFi della Heltec (o la Heltec al WiFi di bordo) e leggere `/api/state` e `/api/messages`.
Funziona per mostrare i bersagli in Signal K, ma la posizione trasmessa resta quella del GPS collegato alla
Heltec: il GPS della barca (Signal K o gpsd) non arriva alla radio. Per questo la strada scelta è il modem.

## 11. Cosa deve fare il nodo Linux per essere compatibile

- Pacchetti, mesh e chat esattamente come nelle sezioni 2–4, compresi tempi, code e regole di
  deduplicazione.
- Allarme come nella sezione 5, con gli stessi profili e i nodi silenziati.
- Stessa API della sezione 8, con i campi del nodo Heltec, così la stessa `web/index.html` e l'app Android
  funzionano senza modifiche; in più le intestazioni CORS per l'app.
- Tracciato e GPX come nella sezione 7.
- Un id a 32 bit stabile (dal modem, o salvato nella configurazione).
- Il tipo di nodo nei `flags` (a differenza del T-Deck).
- Nessuna classe di sonno: il nodo Linux è sempre acceso.
