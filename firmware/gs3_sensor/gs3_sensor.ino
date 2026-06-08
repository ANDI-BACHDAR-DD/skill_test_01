/**
 * ============================================================
 *  CIMES – Chili Intelligent Monitoring & Environmental Sensing
 *  Hardware : ESP32 + GS3 Sensor + Relay Pump + LCD + LEDs
 *  Protocol : SDI-12 → WiFi → XMPP → PostgreSQL
 *
 *  UNIFIED firmware: sensor + pump control + WiFi + XMPP
 *
 *  Libraries required:
 *    - SDI-12          by EnviroDIY (v2.x)
 *    - LiquidCrystal_I2C
 *    - Wire            (built-in)
 *    - WiFi            (built-in ESP32)
 * ============================================================
 */

#include <Arduino.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "SDI12.h"

// ================= WIFI & XMPP CONFIG =================
#define WIFI_SSID        "Alhamd"
#define WIFI_PASSWORD    "13010113"

#define XMPP_SERVER      "10.72.3.255"
#define XMPP_PORT        5222
#define XMPP_JID         "esp32node@localhost"
#define XMPP_PASSWORD    "esp32password"
#define XMPP_TO_JID      "bridge@localhost"

// ================= PIN =================
#define SDI12_PIN   14
#define RELAY_PIN   32
#define LED_GREEN   16
#define LED_YELLOW  17
#define LED_RED     18
#define SDA_PIN     21
#define SCL_PIN     22

// ================= SENSOR =================
#define SENSOR_ADDRESS '0'
#define MAX_RETRY 3

// ================= THRESHOLD (SYNCED) =================
// These match bridge.py & dashboard THRESH values
// VWC stored as decimal (0.0–1.0), converted to % for LCD
#define VWC_PUMP_ON     0.25    // Pompa ON jika VWC < 25%
#define VWC_OPTIMAL_LOW 0.60    // Optimal range start
#define VWC_OPTIMAL_HIGH 0.80   // Pompa OFF jika VWC >= 80%
#define EC_LOW          0.80    // Nutrisi rendah jika EC < 0.8 dS/m
#define TEMP_LOW        18.0    // Suhu rendah
#define TEMP_OPTIMAL_LOW 22.0   // Optimal range start
#define TEMP_OPTIMAL_HIGH 30.0  // Optimal range end
#define TEMP_HIGH       35.0    // Suhu tinggi (kritis)

// ================= TIMER =================
#define PUMP_RUN_MS     7000
#define SOAK_MS         15000
#define READ_INTERVAL_MS 30000UL

// ================= OBJECTS =================
LiquidCrystal_I2C lcd(0x27, 16, 2);
SDI12 sdi12Bus(SDI12_PIN);
WiFiClient xmppClient;

// Custom LCD Character definitions (5x8 pixels)
byte dropletChar[8] = {
  B00100,
  B00100,
  B01010,
  B01010,
  B10001,
  B10001,
  B10001,
  B01110
};
byte tempChar[8] = {
  B00100,
  B01010,
  B01010,
  B01110,
  B01110,
  B11111,
  B11111,
  B01110
};
byte ecChar[8] = {
  B01010,
  B10101,
  B10001,
  B01010,
  B00100,
  B01010,
  B10001,
  B01110
};
byte pumpChar[8] = {
  B00100,
  B01110,
  B11111,
  B00100,
  B00100,
  B11111,
  B01110,
  B00100
};

int lcdPage = 0;
bool pumpState = false;
bool soaking = false;
unsigned long pumpStart = 0;
unsigned long soakStart = 0;

struct GS3Data {
  float vwc;   // m³/m³ (0.0 – 1.0)
  float ec;    // dS/m
  float temp;  // °C
  bool valid;
};

// ================= FORWARD DECLARATIONS =================
bool connectWiFi();
bool connectXMPP();
void sendXMPPMessage(const String &jsonPayload);
void drainXMPP();

// ================= LCD & LED =================
void lcdMsg(String line1, String line2) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1.substring(0, 16));
  lcd.setCursor(0, 1);
  lcd.print(line2.substring(0, 16));
}

void setLED(bool green, bool yellow, bool red) {
  digitalWrite(LED_GREEN, green ? HIGH : LOW);
  digitalWrite(LED_YELLOW, yellow ? HIGH : LOW);
  digitalWrite(LED_RED, red ? HIGH : LOW);
}

// ================= RELAY CONTROL =================
void relayInput(bool active) {
  if (active) {
    pinMode(RELAY_PIN, OUTPUT);
    digitalWrite(RELAY_PIN, LOW);
  } else {
    pinMode(RELAY_PIN, INPUT);
  }
}

void setPump(bool state) {
  if (pumpState == state) return;
  pumpState = state;
  relayInput(state);
  if (state) {
    pumpStart = millis();
    Serial.println("[PUMP] ON");
  } else {
    Serial.println("[PUMP] OFF");
  }
}

void clearSdi12Buffer() {
  while (sdi12Bus.available() > 0) {
    sdi12Bus.read();
  }
}

// ================= SDI-12 SENSOR =================
GS3Data readGS3Once() {
  GS3Data data = {0, 0, 0, false};

  clearSdi12Buffer();
  delay(100);

  // Send M! command
  String cmd = String(SENSOR_ADDRESS) + "M!";
  sdi12Bus.sendCommand(cmd);
  delay(30);

  // Read acknowledge
  String ack = "";
  while (sdi12Bus.available()) {
    char c = sdi12Bus.read();
    if (c == '\n') break;
    if (isprint(c)) ack += c;
  }
  Serial.printf("[SDI-12] M! ack: %s\n", ack.c_str());
  delay(1500);  // Wait longer for sensor to prepare data

  clearSdi12Buffer(); // Clear the service request (e.g., "0\r\n") before sending the D0! request

  // Request data
  String dCmd = String(SENSOR_ADDRESS) + "D0!";
  sdi12Bus.sendCommand(dCmd);
  delay(100);  // Wait longer after D0!

  // Read data response
  String response = "";
  unsigned long t = millis();
  while (millis() - t < 3000) {  // Longer timeout
    if (sdi12Bus.available()) {
      char c = sdi12Bus.read();
      if (c == '\n') break;
      response += c;
    }
  }
  Serial.printf("[SDI-12] D0! raw: %s\n", response.c_str());

  if (response.length() < 5) return data;

  // Parse: 0+VWC+Temp+EC
  response.replace("-", "+-");
  int plus[4];
  int count = 0;
  for (int i = 0; i < (int)response.length() && count < 4; i++) {
    if (response.charAt(i) == '+') plus[count++] = i;
  }
  if (count < 3) return data;

  data.vwc  = response.substring(plus[0]+1, (count>1)?plus[1]:response.length()).toFloat();
  data.temp = response.substring(plus[1]+1, (count>2)?plus[2]:response.length()).toFloat();
  data.ec   = response.substring(plus[2]+1, (count>3)?plus[3]:response.length()).toFloat();

  // Validate ranges
  // VWC comes as percentage (0-100) from GS3, convert to decimal
  if (data.vwc < 0 || data.vwc > 100.0) return data;
  if (data.temp < -40 || data.temp > 60) return data;
  if (data.ec < 0 || data.ec > 10000) return data;  // EC in mS/m
  data.ec = data.ec / 1000.0;  // Convert mS/m to dS/m
  data.vwc = data.vwc / 100.0; // Convert % to decimal (0.0-1.0)

  data.valid = true;
  return data;
}

GS3Data readGS3() {
  GS3Data data = {0, 0, 0, false};
  for (int i = 1; i <= MAX_RETRY; i++) {
    data = readGS3Once();
    if (data.valid) return data;
    Serial.printf("[RETRY] %d\n", i);
    delay(1000);
  }
  return data;
}

// ================= STATUS =================
String getStatus(GS3Data data) {
  if (data.vwc < VWC_PUMP_ON) return "TANAH KERING";
  if (data.vwc < VWC_OPTIMAL_LOW) return "VWC RENDAH";
  if (data.vwc <= VWC_OPTIMAL_HIGH) {
    if (data.ec < EC_LOW && data.temp < TEMP_LOW) return "EC&TEMP LOW";
    if (data.ec < EC_LOW) return "NUTRISI LOW";
    if (data.temp < TEMP_LOW) return "SUHU RENDAH";
    if (data.temp > TEMP_HIGH) return "SUHU TINGGI";
    return "NORMAL";
  }
  return "VWC TINGGI";
}

void updateLED(GS3Data data) {
  if (data.vwc < VWC_PUMP_ON) {
    setLED(false, false, true);       // Merah – kritis
  } else if (data.vwc < VWC_OPTIMAL_LOW || data.vwc > VWC_OPTIMAL_HIGH ||
             data.ec < EC_LOW || data.temp < TEMP_LOW || data.temp > TEMP_HIGH) {
    setLED(false, true, false);       // Kuning – warning
  } else {
    setLED(true, false, false);       // Hijau – optimal
  }
}

void updateLCD(GS3Data data) {
  lcd.clear();
  
  if (lcdPage == 0) {
    // ----------------------------------------------------
    // Halaman 1: Pembacaan Sensor Aktual (Moisture, Temp, EC)
    // ----------------------------------------------------
    // Baris 1: [Droplet Icon] [Kelembaban]%   [Thermometer Icon] [Suhu]°C
    lcd.setCursor(0, 0);
    lcd.write(0); // Droplet icon
    lcd.print(" ");
    lcd.print(data.vwc * 100.0, 1);
    lcd.print("%  ");
    
    lcd.setCursor(9, 0);
    lcd.write(1); // Thermometer icon
    lcd.print(" ");
    lcd.print(data.temp, 1);
    lcd.print("C");

    // Baris 2: [Nutrient Icon] EC: [EC] dS/m
    lcd.setCursor(0, 1);
    lcd.write(2); // Nutrient icon
    lcd.print(" EC: ");
    lcd.print(data.ec, 2);
    lcd.print(" dS/m");

    lcdPage = 1; // Pindah halaman pada loop berikutnya
  } else {
    // ----------------------------------------------------
    // Halaman 2: Status Sistem & Kontrol Pompa
    // ----------------------------------------------------
    // Baris 1: STATUS: [Teks Status]
    lcd.setCursor(0, 0);
    lcd.print("STS: ");
    lcd.print(getStatus(data));

    // Baris 2: [Pump Icon] POMPA: [AKTIF / STANDBY]
    lcd.setCursor(0, 1);
    lcd.write(3); // Pump icon
    lcd.print(" PMP: ");
    if (pumpState) {
      lcd.print("AKTIF (ON)");
    } else {
      lcd.print("STDBY (OFF)");
    }

    lcdPage = 0; // Pindah kembali ke halaman 1
  }
}

void printSerial(GS3Data data) {
  Serial.println("================================");
  Serial.printf("VWC    : %.4f m3/m3 (%.1f%%)\n", data.vwc, data.vwc*100);
  Serial.printf("EC     : %.4f dS/m\n", data.ec);
  Serial.printf("Temp   : %.1f C\n", data.temp);
  Serial.printf("Pump   : %s\n", pumpState ? "ON" : "OFF");
  Serial.printf("Status : %s\n", getStatus(data).c_str());
  Serial.println("================================");
}

// ================= JSON BUILDER =================
String buildJSON(GS3Data data) {
  char buf[128];
  snprintf(buf, sizeof(buf),
           "{\"vwc\":%.4f,\"temp\":%.2f,\"ec\":%.4f}",
           data.vwc, data.temp, data.ec);
  return String(buf);
}

// ================= HTTP SEND DATA =================
void sendSensorData(GS3Data data) {
  // Construct the sensor reading payload matching SensorReading pydantic model in FastAPI
  // {"vwc": vwc, "temp": temp, "ec": ec, "node_id": "esp32-node-01"}
  String payload = "{\"vwc\":" + String(data.vwc, 4) + 
                   ",\"temp\":" + String(data.temp, 2) + 
                   ",\"ec\":" + String(data.ec, 4) + 
                   ",\"node_id\":\"esp32-node-01\"}";
  Serial.printf("[PAYLOAD] %s\n", payload.c_str());

  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    // Connect directly to backend served on uvicorn
    String url = "http://" + String(XMPP_SERVER) + ":8000/api/ingest"; 
    Serial.printf("[HTTP] Connecting to: %s\n", url.c_str());
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    
    int httpResponseCode = http.POST(payload);
    
    if (httpResponseCode > 0) {
      String response = http.getString();
      Serial.printf("[HTTP] Success! Response code: %d, Response: %s\n", httpResponseCode, response.c_str());
    } else {
      Serial.printf("[HTTP] Error sending POST: %s\n", http.errorToString(httpResponseCode).c_str());
    }
    http.end();
  } else {
    Serial.println("[HTTP] Not connected to Wi-Fi – data saved locally only");
  }
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);

  relayInput(false);
  pumpState = false;

  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_YELLOW, OUTPUT);
  pinMode(LED_RED, OUTPUT);
  setLED(false, false, true);

  Wire.begin(SDA_PIN, SCL_PIN);
  lcd.init();
  lcd.backlight();
  lcd.createChar(0, dropletChar);
  lcd.createChar(1, tempChar);
  lcd.createChar(2, ecChar);
  lcd.createChar(3, pumpChar);

  sdi12Bus.begin();
  delay(500);

  lcdMsg("CIMES SmartFarm", "Booting...");
  Serial.println("\n================================");
  Serial.println("CIMES – Smart Farming Cabai");
  Serial.println("================================");
  Serial.printf("VWC: ON<%.0f%% OFF>=%.0f%%\n", VWC_PUMP_ON*100, VWC_OPTIMAL_HIGH*100);
  Serial.printf("Temp Optimal: %.0f-%.0fC\n", TEMP_OPTIMAL_LOW, TEMP_OPTIMAL_HIGH);
  Serial.printf("EC Optimal: >= %.1f dS/m\n", EC_LOW);

  // Connect WiFi
  connectWiFi();

  lcdMsg("CIMES Ready", WiFi.status()==WL_CONNECTED ? "WiFi OK" : "WiFi FAIL");
  delay(2000);
}

// ================= LOOP =================
void loop() {
  // 1. Fase pompa ON
  if (pumpState) {
    if (millis() - pumpStart >= PUMP_RUN_MS) {
      setPump(false);
      soaking = true;
      soakStart = millis();
      lcdMsg("PUMP OFF", "WATER SOAKING");
      Serial.println("[INFO] Pompa OFF, tunggu air meresap");
    } else {
      lcdMsg("PUMP ON", "NO SENSOR READ");
    }
    delay(1000);
    return;
  }

  // 2. Fase soak
  if (soaking) {
    if (millis() - soakStart < SOAK_MS) {
      lcdMsg("SOAKING", "WAIT SENSOR");
      delay(1000);
      return;
    }
    soaking = false;
    Serial.println("[INFO] Fase meresap selesai");
  }

  // 3. Baca sensor
  GS3Data data = readGS3();

  if (!data.valid) {
    setPump(false);
    setLED(false, false, true);
    lcdMsg("Sensor Error", "Pump OFF");
    Serial.println("[ERROR] Sensor gagal dibaca");
    delay(3000);
    return;
  }

  // 4. Kontrol pompa (hysteresis)
  if (data.vwc < VWC_PUMP_ON) {
    setPump(true);
  } else if (data.vwc >= VWC_OPTIMAL_HIGH) {
    setPump(false);
  }

  // 5. Update display & status
  updateLED(data);
  updateLCD(data);
  printSerial(data);

  // 6. Kirim data langsung ke Backend API via HTTP POST
  sendSensorData(data);

  delay(3000);
}

// ================= WIFI =================
bool connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;

  Serial.printf("[WiFi] Connecting to %s ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000UL) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WiFi] Connected – IP: %s\n", WiFi.localIP().toString().c_str());
    return true;
  }
  Serial.println("[WiFi] Connection FAILED");
  return false;
}

// ================= XMPP =================
static const char B64_CHARS[] =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

String base64Encode(const uint8_t *data, size_t len) {
  String out;
  out.reserve(((len + 2) / 3) * 4 + 1);
  for (size_t i = 0; i < len; i += 3) {
    uint8_t b0 = data[i];
    uint8_t b1 = (i+1 < len) ? data[i+1] : 0;
    uint8_t b2 = (i+2 < len) ? data[i+2] : 0;
    out += B64_CHARS[(b0 >> 2) & 0x3F];
    out += B64_CHARS[((b0 & 0x03) << 4) | ((b1 >> 4) & 0x0F)];
    out += (i+1 < len) ? B64_CHARS[((b1 & 0x0F) << 2) | ((b2 >> 6) & 0x03)] : '=';
    out += (i+2 < len) ? B64_CHARS[b2 & 0x3F] : '=';
  }
  return out;
}

String buildSaslPlain(const char *jid, const char *password) {
  String jidStr = String(jid);
  int atIdx = jidStr.indexOf('@');
  String username = (atIdx >= 0) ? jidStr.substring(0, atIdx) : jidStr;
  
  size_t uLen = username.length();
  size_t passLen = strlen(password);
  size_t totalLen = 1 + uLen + 1 + passLen;
  
  uint8_t *buf = (uint8_t *)malloc(totalLen);
  if (!buf) return "";
  
  buf[0] = '\0';
  memcpy(buf + 1, username.c_str(), uLen);
  buf[1 + uLen] = '\0';
  memcpy(buf + 1 + uLen + 1, password, passLen);
  
  String out = base64Encode(buf, totalLen);
  free(buf);
  return out;
}

bool waitForToken(const char *token, unsigned long timeoutMs = 10000) {
  String buf;
  unsigned long start = millis();
  Serial.printf("[XMPP] Waiting for: %s (timeout %lu ms)\n", token, timeoutMs);
  while (millis() - start < timeoutMs) {
    while (xmppClient.available()) {
      char c = xmppClient.read();
      Serial.print(c); // Print raw character to serial for debugging
      buf += c;
      if (buf.indexOf(token) >= 0) {
        Serial.println("\n[XMPP] Found expected token!");
        return true;
      }
    }
    delay(10);
  }
  Serial.printf("\n[XMPP] Timeout waiting for: %s\n", token);
  Serial.printf("[XMPP] Buffer accumulated: %s\n", buf.c_str());
  return false;
}

bool connectXMPP() {
  Serial.printf("[XMPP] Connecting to %s:%d ...\n", XMPP_SERVER, XMPP_PORT);

  if (!xmppClient.connect(XMPP_SERVER, XMPP_PORT)) {
    Serial.println("[XMPP] TCP connect failed!");
    return false;
  }
  Serial.println("[XMPP] TCP connected successfully!");

  String jid = String(XMPP_JID);
  String domain = jid.substring(jid.indexOf('@') + 1);

  String openStream =
    "<?xml version='1.0'?>"
    "<stream:stream "
      "to='" + domain + "' "
      "xmlns='jabber:client' "
      "xmlns:stream='http://etherx.jabber.org/streams' "
      "version='1.0'>";
  Serial.println("[XMPP] Sending stream opening...");
  xmppClient.print(openStream);

  if (!waitForToken("<stream:features")) { xmppClient.stop(); return false; }

  String saslPayload = buildSaslPlain(XMPP_JID, XMPP_PASSWORD);
  Serial.println("[XMPP] Sending SASL AUTH PLAIN...");
  xmppClient.print("<auth xmlns='urn:ietf:params:xml:ns:xmpp-sasl' mechanism='PLAIN'>" + saslPayload + "</auth>");

  if (!waitForToken("<success", 15000)) { xmppClient.stop(); return false; }
  Serial.println("[XMPP] SASL PLAIN auth OK");

  Serial.println("[XMPP] Re-opening stream...");
  xmppClient.print(openStream);
  if (!waitForToken("<stream:features")) { xmppClient.stop(); return false; }

  Serial.println("[XMPP] Sending resource bind...");
  xmppClient.print("<iq type='set' id='bind1'><bind xmlns='urn:ietf:params:xml:ns:xmpp-bind'><resource>esp32</resource></bind></iq>");
  if (!waitForToken("bind")) { xmppClient.stop(); return false; }

  Serial.println("[XMPP] Establishing session...");
  xmppClient.print("<iq type='set' id='sess1'><session xmlns='urn:ietf:params:xml:ns:xmpp-session'/></iq>");
  delay(200);
  drainXMPP();

  Serial.println("[XMPP] Connected and authenticated successfully!");
  return true;
}

void sendXMPPMessage(const String &jsonPayload) {
  String escaped = jsonPayload;
  escaped.replace("&", "&amp;");
  String stanza = "<message to='" + String(XMPP_TO_JID) + "' type='chat'><body>" + escaped + "</body></message>";
  xmppClient.print(stanza);
  Serial.printf("[XMPP] Sent (%d bytes)\n", stanza.length());
}

void drainXMPP() {
  while (xmppClient.available()) xmppClient.read();
}
