// NavPlotter modem - Heltec WiFi LoRa 32 V2 connected to a Raspberry Pi over USB
//
// The board is only a radio. It receives every LoRa packet on the NavPlotter channel and hands it to the
// Raspberry Pi, and transmits the bytes the Raspberry Pi sends. Mesh, chat, alarms and web app run on the
// Raspberry Pi. The radio settings come from include/traffic_packet.h, the same file as every other NavPlotter.
//
// Serial port: 115200 baud, lines ending in \n, packets in hexadecimal (see docs/PROTOCOL.md, section 10).
//   modem -> Pi   READY <version> <id> <MHz> <SF> <dBm>   at start-up and after INFO
//                 RX <hex> <rssi> <snr>                    a packet received with a good CRC
//                 TXOK | TXERR <reason> | BUSY | DUTY      the outcome of a TX
//                 # <text>                                 diagnostics, to be ignored
//   Pi -> modem   TX <hex>                                 transmit these bytes (1-255)
//                 INFO                                     say READY again
//                 DUTY?                                    "# duty <ms used> <ms allowed>" for the last hour
#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include "traffic_packet.h"

#define FIRMWARE_VERSION "0.1.0"

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

// The 869.4-869.65 MHz band allows 10% of the time on air: 360 s in any hour, counted in one-minute slots
#define DUTY_SLOTS 60
#define DUTY_LIMIT_MS 360000UL
#define TX_TIMEOUT_MS 3000UL
#define LINE_MAX 600      // "TX " + 255 bytes in hexadecimal, with room to spare

SX1276 radio = new Module(PIN_LORA_NSS, PIN_LORA_DIO0, PIN_LORA_RST, PIN_LORA_DIO1);

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

void printReady() {
  Serial.printf("READY %s %08lX %.3f %d %d\n", FIRMWARE_VERSION, (unsigned long)myId, LORA_FREQ, LORA_SF, LORA_POWER_DBM);
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
  if(hex.length() % 2 != 0 || len == 0 || len > sizeof(buf)) { Serial.println("TXERR length"); return; }
  for(size_t i = 0; i < len; i++) {
    int hi = hexValue(hex[2 * i]), lo = hexValue(hex[2 * i + 1]);
    if(hi < 0 || lo < 0) { Serial.println("TXERR hex"); return; }
    buf[i] = (hi << 4) | lo;
  }
  if(!radioOk) { Serial.println("TXERR radio"); return; }
  if(transmitting) { Serial.println("BUSY"); return; }
  uint32_t airMs = (radio.getTimeOnAir(len) + 999) / 1000;
  if(dutyUsed() + airMs > DUTY_LIMIT_MS) { Serial.println("DUTY"); return; }

  // The radio may have raised its "done" signal for a packet that came in meanwhile: it is cleared, or the
  // transmission would look finished at once
  radio.standby();
  radioIrq = false;
  int state = radio.startTransmit(buf, len);
  if(state != RADIOLIB_ERR_NONE) {
    Serial.printf("TXERR %d\n", state);
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
  else if(line == "DUTY?") Serial.printf("# duty %lu %lu\n", (unsigned long)dutyUsed(), DUTY_LIMIT_MS);
  else if(line.length()) Serial.println("# unknown command");
}

void handleSerial() {
  static String line;
  static bool tooLong = false;
  while(Serial.available()) {
    char c = Serial.read();
    if(c == '\n' || c == '\r') {
      if(tooLong) Serial.println("TXERR length");
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

// ---- Radio ----

void initRadio() {
  SPI.begin(PIN_LORA_SCK, PIN_LORA_MISO, PIN_LORA_MOSI, PIN_LORA_NSS);
  // SX1276: no TCXO and no antenna switch to set up; "done" for both sending and receiving comes on DIO0
  int state = radio.begin(LORA_FREQ, LORA_BW, LORA_SF, LORA_CR, LORA_SYNC, LORA_POWER_DBM, LORA_PREAMBLE, 0);
  if(state == RADIOLIB_ERR_NONE) {
    radio.setCurrentLimit(140);
    radio.setCRC(true);
    radio.setDio0Action(onRadioIrq, RISING);
    radioOk = radio.startReceive() == RADIOLIB_ERR_NONE;
  }
  if(!radioOk) Serial.printf("# LoRa start failed (%d)\n", state);
}

void handleRadio() {
  if(!radioOk) return;

  if(radioIrq) {
    radioIrq = false;
    if(transmitting) {
      radio.finishTransmit();
      transmitting = false;
      Serial.println("TXOK");
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
        Serial.print(out);
        ledFlash();
      } else {
        Serial.printf("# RX dropped (%d)\n", state);
      }
    }
    radio.startReceive();
  }

  // Transmission stuck: go back to receiving
  if(transmitting && millis() - txStart > TX_TIMEOUT_MS) {
    radio.finishTransmit();
    transmitting = false;
    Serial.println("TXERR timeout");
    radio.startReceive();
  }
}

void setup() {
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
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
  if(ledOff && (long)(millis() - ledOff) >= 0) { digitalWrite(PIN_LED, LOW); ledOff = 0; }
}
