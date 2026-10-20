# Modem NavPlotter per Heltec WiFi LoRa 32 V2 e V4

Firmware che trasforma una **Heltec WiFi LoRa 32 V2** (ESP32 + SX1276, 868 MHz) in un modem LoRa per il
Raspberry Pi, collegato via USB. La scheda fa solo da radio: riceve ogni pacchetto sul canale NavPlotter
(869,525 MHz, SF9, BW 125 kHz, sync 0x4E, gli stessi parametri di tutti i nodi, da
`include/traffic_packet.h`) e trasmette i byte che le manda il Raspberry. Mesh, chat, allarmi e web app
girano sul Raspberry.

Il protocollo della seriale è descritto in `docs/PROTOCOL.md`, sezione 10, e in cima a `src/main.cpp`.

## Caricare il firmware

Dal computer, con la Heltec collegata via USB:

```
cd firmware/heltec-modem
~/.platformio/penv/bin/pio run -e heltec_wifi_lora_32_V2 -t upload   # Heltec V2
~/.platformio/penv/bin/pio run -e heltec_wifi_lora_32_V4 -t upload   # Heltec V4
```

L'immagine già compilata di ognuna (`heltec-v2-modem.bin`, `heltec-v4-modem.bin`, da scrivere all'indirizzo 0) è
tra gli "artifacts" dell'ultima esecuzione di *Firmware* nella scheda Actions di GitHub. Se la V4 non si fa
programmare, tieni premuto il tasto **BOOT** (PRG) mentre la colleghi.

## Heltec V4

- Radio SX1262 con l'amplificatore della V4 (GC1109 sulla V4.2, KCT8103L sulla V4.3, riconosciuto da solo):
  22 dBm all'antenna, come i nodi Heltec V3.
- Il modulo GPS L76K, se collegato al connettore GPS, viene acceso e le sue frasi RMC e GGA arrivano al
  Raspberry come righe `GPS <frase>`: il nodo Linux le usa come posizione propria se non c'è un GPS USB.
- Parla con il Raspberry attraverso la porta USB dell'ESP32-S3, che sul Raspberry compare come
  `/dev/ttyACM0` (`/dev/serial/by-id/usb-Espressif...`). Il nodo Linux la trova da solo.
- Aprendo la porta la V4 non si riavvia: il nodo Linux manda `INFO` e aspetta `READY`.

## Provarlo sul Raspberry Pi

1. Collega la Heltec al Raspberry con il cavo USB e l'antenna montata (mai trasmettere senza antenna).
2. Installa pyserial e dai all'utente l'accesso alla seriale (poi esci e rientra):
   ```
   sudo apt install python3-serial
   sudo usermod -aG dialout $USER
   ```
3. Solo ascolto, per vedere Pippa, il T-Deck e gli altri nodi:
   ```
   python3 tools/modem_test.py --port /dev/ttyUSB0
   ```
4. Ascolto e trasmissione di una posizione fissa ogni 10 s, per vedere il Raspberry sul T-Deck:
   ```
   python3 tools/modem_test.py --port /dev/ttyUSB0 --name RASPI --lat 45.4637 --lon 12.1471
   ```

All'apertura della porta la Heltec si riavvia (il circuito di caricamento automatico usa DTR/RTS): lo
strumento aspetta la riga `READY`. Lo strumento di prova non ritrasmette i pacchetti degli altri.

Si può anche parlare al modem a mano, per esempio con `screen /dev/ttyUSB0 115200`: scrivendo `INFO` il
modem risponde con `READY`, `DUTY?` dice quanto tempo di trasmissione è stato usato nell'ultima ora.

## Cosa fa

- `RX <esadecimale> <rssi> <snr>` per ogni pacchetto ricevuto con CRC buono, senza filtri;
- `TX <esadecimale>` trasmette da 1 a 255 byte e risponde `TXOK`, `TXERR <motivo>`, `BUSY` (sta già
  trasmettendo) o `DUTY` (superato il 10% di tempo in aria nell'ultima ora, il limite della banda);
- l'id del nodo è calcolato dal MAC con la stessa formula degli altri nodi ESP32 e compare nella riga
  `READY`;
- il LED bianco fa un breve lampo a ogni pacchetto ricevuto o trasmesso;
- il display non viene usato.

## Nome fisso per la porta (facoltativo)

La V2 ha un convertitore CP2102. Una regola udev le dà un nome che non cambia:

```
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="navplotter-modem", GROUP="dialout"' | sudo tee /etc/udev/rules.d/99-navplotter-modem.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Poi si usa `--port /dev/navplotter-modem`.
