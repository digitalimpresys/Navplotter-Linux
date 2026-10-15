# Modem NavPlotter per Heltec WiFi LoRa 32 V2

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
~/.platformio/penv/bin/pio run -t upload
```

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
