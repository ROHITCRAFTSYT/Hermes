/*
 * Hermes custom node — MQTT relay/fan/light node.
 * Flash to a spare ESP8266 or ESP32 wired to a relay module.
 *
 * Subscribes to  hermes/<NODE_NAME>/cmd  and reacts to "on" / "off" / "toggle".
 * Say to Hermes: "turn on the fan node" -> the agent calls mqtt_command("fan","on").
 *
 * Needs libraries (Arduino Library Manager):
 *   • PubSubClient  (by Nick O'Leary)
 * For ESP8266 use <ESP8266WiFi.h>; for ESP32 use <WiFi.h> (swap the include).
 */

#include <ESP8266WiFi.h>          // ESP32: replace with <WiFi.h>
#include <PubSubClient.h>

const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* MQTT_HOST     = "192.168.1.50";   // the PC running Mosquitto
const int   MQTT_PORT     = 1883;

const char* NODE_NAME     = "fan";            // -> topic hermes/fan/cmd
const int   RELAY_PIN     = 5;                // GPIO driving the relay
const bool  RELAY_ACTIVE_HIGH = true;

WiFiClient net;
PubSubClient mqtt(net);
String topicCmd = String("hermes/") + NODE_NAME + "/cmd";
bool state = false;

void applyRelay() {
  digitalWrite(RELAY_PIN, (state == RELAY_ACTIVE_HIGH) ? HIGH : LOW);
}

void onMessage(char* topic, byte* payload, unsigned int len) {
  String cmd;
  for (unsigned int i = 0; i < len; i++) cmd += (char)payload[i];
  cmd.trim(); cmd.toLowerCase();
  if (cmd == "on")     state = true;
  else if (cmd == "off")    state = false;
  else if (cmd == "toggle") state = !state;
  applyRelay();
  mqtt.publish((String("hermes/") + NODE_NAME + "/state").c_str(), state ? "on" : "off", true);
  Serial.printf("[%s] -> %s\n", NODE_NAME, state ? "ON" : "OFF");
}

void reconnect() {
  while (!mqtt.connected()) {
    String id = String("hermes-") + NODE_NAME + "-" + String(ESP.getChipId(), HEX);
    if (mqtt.connect(id.c_str())) {
      mqtt.subscribe(topicCmd.c_str());
      Serial.printf("[MQTT] connected, subscribed to %s\n", topicCmd.c_str());
    } else {
      Serial.printf("[MQTT] retry in 2s (state=%d)\n", mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  applyRelay();
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) { delay(400); Serial.print('.'); }
  Serial.printf("\n[WiFi] %s\n", WiFi.localIP().toString().c_str());
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMessage);
}

void loop() {
  if (!mqtt.connected()) reconnect();
  mqtt.loop();
}
