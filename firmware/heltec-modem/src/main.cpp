// NavPlotter modem - Heltec WiFi LoRa 32 V2 or V4 connected to a Raspberry Pi over USB
//
// The board is only a radio (the V4 also passes on its GPS). It receives every LoRa packet on the NavPlotter channel and hands it to the
// Raspberry Pi, and transmits the bytes the Raspberry Pi sends. Mesh, chat, alarms and web app run on the
// Raspberry Pi. The radio settings come from include/traffic_packet.h, the same file as every other NavPlotter.
//
// Serial port: 115200 baud, lines ending in \n, packets in hexadecimal (see docs/PROTOCOL.md, section 10).
//   modem -> Pi   READY <version> <id> <MHz> <SF> <dBm>   at start-up and after INFO
//                 RX <hex> <rssi> <snr>                    a packet received with a good CRC
//                 TXOK | TXERR <reason> | BUSY | DUTY      the outcome of a TX
//                 GPS <NMEA sentence>                      V4 with its GPS module: the RMC and GGA sentences
//                 # <text>                                 diagnostics, to be ignored
//   Pi -> modem   TX <hex>                                 transmit these bytes (1-255)
//                 INFO                                     say READY again
//                 DUTY?                                    "# duty <ms used> <ms allowed>" for the last hour
#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include "traffic_packet.h"

#define FIRMWARE_VERSION "0.2.0"

#ifdef BOARD_HELTEC_V4
// ---- Heltec WiFi LoRa 32 V4 pins (ESP32-S3, SX1262 with a power amplifier, L76K GPS module) ----
#define PIN_LORA_NSS 8
#define PIN_LORA_SCK 9
#define PIN_LORA_MOSI 10
#define PIN_LORA_MISO 11
#define PIN_LORA_RST 12
#define PIN_LORA_BUSY 13
#define PIN_LORA_DIO1 14
#define PIN_LED 35        // white LED: a short flash for every packet sent or received
#define PIN_VEXT 36       // LOW = power to the display and the GPS connector
// Radio front end: V4.2 has a GC1109, V4.3 a KCT8103L; both amplify what the SX1262 gives and both are
// switched between sending and receiving by the SX1262 DIO2
#define PIN_FEM_POWER 7   // HIGH = power to the front end
#define PIN_FEM_CSD 2     // HIGH = front end on; read before it is driven, HIGH tells a KCT8103L
#define PIN_FEM_CTX 5     // KCT8103L: LOW = receive through the low-noise amplifier
#define PIN_FEM_CPS 46    // GC1109: LOW, DIO2 does the switching
#define LORA_POWER_DBM 22 // at the antenna, as the Heltec V3 nodes
#define GPS_BAUD 9600
#define PIN_GPS_TX 38     // board TX -> GPS RX
#define PIN_GPS_RX 39     // board RX <- GPS TX
#define PIN_GPS_EN 34     // LOW = GPS on
#define PIN_GPS_RESET 42  // LOW = GPS held in reset
#define PIN_GPS_WAKE 40   // HIGH = GPS kept awake
#else
// ---- Heltec WiFi LoRa 32 V2 pins ----
#define PIN_LORA_NSS 18
#define PIN_LORA_SCK 5
#define PIN_LORA_MOSI 27
#define PIN_LORA_MISO 19
#define PIN_LORA_RST 14
#define PIN_LORA_DIO0 26
#define PIN_LORA_DIO1 35
#define PIN_LED 25        // white LED: a short flash for every packet sent or received
#define LORA_POWER_DBM 20 // the most the SX1276 gives
#endif

// The 869.4-869.65 MHz band allows 10% of the time on air: 360 s in any hour, counted in one-minute slots
#define DUTY_SLOTS 60
#define DUTY_LIMIT_MS 360000UL
#define TX_TIMEOUT_MS 3000UL
#define LINE_MAX 600      // "TX " + 255 bytes in hexadecimal, with room to spare

#ifdef BOARD_HELTEC_V4
SX1262 radio = new Module(PIN_LORA_NSS, PIN_LORA_DIO1, PIN_LORA_RST, PIN_LORA_BUSY);
#else
SX1276 radio = new Module(PIN_LORA_NSS, PIN_LORA_DIO0, PIN_LORA_RST, PIN_LORA_DIO1);
#endif

uint32_t myId = 0;
bool radioOk = false;
volatile bool radioIrq = false;
bool transmitting = false;
unsigned long txStart = 0, ledOff = 0;
uint32_t dutyMs[DUTY_SLOTS] = {0};
uint32_t dutyMinute = 0;

void IRAM_ATTR onRadioIrq() {
  radioIrq = true;
}

void ledFlash() {
  digitalWrite(PIN_LED, HIGH);
  ledOff = millis() + 30;
}

// ---- Duty cycle ----

// Moves the one-minute slots forward to the present: the slots of the minutes gone by are emptied
void dutyAdvance() {
  uint32_t minute = millis() / 60000UL;
  for(uint32_t steps = 0; dutyMinute < minute && steps < DUTY_SLOTS; steps++) dutyMs[++dutyMinute % DUTY_SLOTS] = 0;
  dutyMinute = minute;
}

uint32_t dutyUsed() {
  dutyAdvance();
  uint32_t sum = 0;
  for(uint32_t ms : dutyMs) sum += ms;
  return sum;
}

// ---- Serial ----

// The V4 is built with the ESP32-S3 USB port as Serial; UART0 (Serial0) gets the same lines, so the modem
// answers on whichever port reaches the Raspberry Pi
void hostWrite(const char* text) {
  Serial.print(text);
#ifdef BOARD_HELTEC_V4
  Serial0.print(text);
#endif
}

void hostPrintf(const char* fmt, ...) {
  char buf[640];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  hostWrite(buf);
}

void printReady() {
  hostPrintf("READY %s %08lX %.3f %d %d\n", FIRMWARE_VERSION, (unsigned long)myId, LORA_FREQ, LORA_SF, LORA_POWER_DBM);
}

int hexValue(char c) {
  if(c >= '0' && c <= '9') return c - '0';
  if(c >= 'a' && c <= 'f') return c - 'a' + 10;
  if(c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

void handleTx(const String& hex) {
  uint8_t buf[255];
  size_t len = hex.length() / 2;
  if(hex.length() % 2 != 0 || len == 0 || len > sizeof(buf)) { hostWrite("TXERR length\n"); return; }
  for(size_t i = 0; i < len; i++) {
    int hi = hexValue(hex[2 * i]), lo = hexValue(hex[2 * i + 1]);
    if(hi < 0 || lo < 0) { hostWrite("TXERR hex\n"); return; }
    buf[i] = (hi << 4) | lo;
  }
  if(!radioOk) { hostWrite("TXERR radio\n"); return; }
  if(transmitting) { hostWrite("BUSY\n"); return; }
  uint32_t airMs = (radio.getTimeOnAir(len) + 999) / 1000;
  if(dutyUsed() + airMs > DUTY_LIMIT_MS) { hostWrite("DUTY\n"); return; }

  // The radio may have raised its "done" signal for a packet that came in meanwhile: it is cleared, or the
  // transmission would look finished at once
  radio.standby();
  radioIrq = false;
  int state = radio.startTransmit(buf, len);
  if(state != RADIOLIB_ERR_NONE) {
    hostPrintf("TXERR %d\n", state);
    radio.startReceive();
    return;
  }
  dutyMs[dutyMinute % DUTY_SLOTS] += airMs;
  transmitting = true;
  txStart = millis();
  ledFlash();
}

void handleLine(String line) {
  line.trim();
  if(line.startsWith("TX ")) handleTx(line.substring(3));
  else if(line == "INFO") printReady();
  else if(line == "DUTY?") hostPrintf("# duty %lu %lu\n", (unsigned long)dutyUsed(), DUTY_LIMIT_MS);
  else if(line.length()) hostWrite("# unknown command\n");
}

struct LineReader {
  String line;
  bool tooLong = false;

  void read(Stream& port) {
    while(port.available()) {
      char c = port.read();
      if(c == '\n' || c == '\r') {
        if(tooLong) hostWrite("TXERR length\n");
        else handleLine(line);
        line = "";
        tooLong = false;
      } else if(line.length() < LINE_MAX) {
        line += c;
      } else {
        tooLong = true;
      }
    }
  }
};

LineReader usbLine;
#ifdef BOARD_HELTEC_V4
LineReader uartLine;
#endif

void handleSerial() {
  usbLine.read(Serial);
#ifdef BOARD_HELTEC_V4
  uartLine.read(Serial0);
#endif
}

// ---- GPS (V4) ----

#ifdef BOARD_HELTEC_V4
void initGps() {
  pinMode(PIN_GPS_EN, OUTPUT);
  digitalWrite(PIN_GPS_EN, LOW);
  pinMode(PIN_GPS_RESET, OUTPUT);
  digitalWrite(PIN_GPS_RESET, HIGH);
  pinMode(PIN_GPS_WAKE, OUTPUT);
  digitalWrite(PIN_GPS_WAKE, HIGH);
  Serial1.begin(GPS_BAUD, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
}

// Passes on the RMC (position, speed, course, time) and GGA (satellites, HDOP) sentences, the ones the
// Raspberry Pi reads, one whole line at a time
void handleGps() {
  static char line[100];
  static size_t len = 0;
  while(Serial1.available()) {
    char c = Serial1.read();
    if(c == '\n' || c == '\r') {
      line[len] = 0;
      if(len > 6 && line[0] == '$' && (!strncmp(line + 3, "RMC", 3) || !strncmp(line + 3, "GGA", 3))) {
        hostPrintf("GPS %s\n", line);
      }
      len = 0;
    } else if(len < sizeof(line) - 1) {
      line[len++] = c;
    } else {
      len = 0;    // not NMEA: thrown away
    }
  }
}
#endif

// ---- Radio ----

#ifdef BOARD_HELTEC_V4
// Switches the front end on and says how much it amplifies (dB), to ask the SX1262 for that much less
int initFrontEnd() {
  pinMode(PIN_FEM_POWER, OUTPUT);
  digitalWrite(PIN_FEM_POWER, HIGH);
  delay(5);
  pinMode(PIN_FEM_CSD, INPUT);
  bool kct = digitalRead(PIN_FEM_CSD) == HIGH;
  pinMode(PIN_FEM_CSD, OUTPUT);
  digitalWrite(PIN_FEM_CSD, HIGH);
  if(kct) {
    pinMode(PIN_FEM_CTX, OUTPUT);
    digitalWrite(PIN_FEM_CTX, LOW);
  } else {
    pinMode(PIN_FEM_CPS, OUTPUT);
    digitalWrite(PIN_FEM_CPS, LOW);
  }
  hostPrintf("# front end %s\n", kct ? "KCT8103L" : "GC1109");
  return kct ? 13 : 11;   // gain at low input power, as measured for Meshtastic
}
#endif

void initRadio() {
  SPI.begin(PIN_LORA_SCK, PIN_LORA_MISO, PIN_LORA_MOSI, PIN_LORA_NSS);
#ifdef BOARD_HELTEC_V4
  int chipDbm = LORA_POWER_DBM - initFrontEnd();
  // TCXO powered from DIO3 at 1.8 V, antenna path switched by DIO2, "done" on DIO1
  int state = radio.begin(LORA_FREQ, LORA_BW, LORA_SF, LORA_CR, LORA_SYNC, chipDbm, LORA_PREAMBLE, 1.8);
  if(state == RADIOLIB_ERR_NONE) {
    radio.setDio2AsRfSwitch(true);
    radio.setCurrentLimit(140);
    radio.setCRC(2);
    radio.setDio1Action(onRadioIrq);
    radioOk = radio.startReceive() == RADIOLIB_ERR_NONE;
  }
#else
  // SX1276: no TCXO and no antenna switch to set up; "done" for both sending and receiving comes on DIO0
  int state = radio.begin(LORA_FREQ, LORA_BW, LORA_SF, LORA_CR, LORA_SYNC, LORA_POWER_DBM, LORA_PREAMBLE, 0);
  if(state == RADIOLIB_ERR_NONE) {
    radio.setCurrentLimit(140);
    radio.setCRC(true);
    radio.setDio0Action(onRadioIrq, RISING);
    radioOk = radio.startReceive() == RADIOLIB_ERR_NONE;
  }
#endif
  if(!radioOk) hostPrintf("# LoRa start failed (%d)\n", state);
}

void handleRadio() {
  if(!radioOk) return;

  if(radioIrq) {
    radioIrq = false;
    if(transmitting) {
      radio.finishTransmit();
      transmitting = false;
      hostWrite("TXOK\n");
    } else {
      uint8_t buf[256];
      size_t len = radio.getPacketLength();
      int state = len > 0 && len <= sizeof(buf) ? radio.readData(buf, len) : RADIOLIB_ERR_PACKET_TOO_LONG;
      if(state == RADIOLIB_ERR_NONE) {
        // One line, written at once, so that the Raspberry Pi never reads half of it
        char out[8 + 2 * sizeof(buf) + 24];
        size_t n = snprintf(out, sizeof(out), "RX ");
        for(size_t i = 0; i < len; i++) n += snprintf(out + n, sizeof(out) - n, "%02X", buf[i]);
        snprintf(out + n, sizeof(out) - n, " %.0f %.1f\n", radio.getRSSI(), radio.getSNR());
        hostWrite(out);
        ledFlash();
      } else {
        hostPrintf("# RX dropped (%d)\n", state);
      }
    }
    radio.startReceive();
  }

  // Transmission stuck: go back to receiving
  if(transmitting && millis() - txStart > TX_TIMEOUT_MS) {
    radio.finishTransmit();
    transmitting = false;
    hostWrite("TXERR timeout\n");
    radio.startReceive();
  }
}

void setup() {
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
#ifdef BOARD_HELTEC_V4
  Serial0.setRxBufferSize(1024);
  Serial0.begin(115200);
  pinMode(PIN_VEXT, OUTPUT);
  digitalWrite(PIN_VEXT, LOW);
  initGps();
#endif
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  // Same node id as every other ESP32 NavPlotter, from the MAC address
  uint64_t mac = ESP.getEfuseMac();
  myId = (uint32_t)(mac >> 16) ^ (uint32_t)mac;
  initRadio();
  printReady();
}

void loop() {
  handleSerial();
  handleRadio();
#ifdef BOARD_HELTEC_V4
  handleGps();
#endif
  if(ledOff && (long)(millis() - ledOff) >= 0) { digitalWrite(PIN_LED, LOW); ledOff = 0; }
}
