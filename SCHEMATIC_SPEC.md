# Hermes Satellite — schematic spec (paste into schematik.io)

Note: this schematic is for the ESP32-S3 wiring option. The currently active
`firmware/hermes_satellite/hermes_satellite.ino` sketch is a classic ESP32
push-to-talk build and overrides the pin map with classic ESP32-safe GPIOs.

Prompt to give the AI, followed by the exact component + wiring spec. Pins match
the firmware in `firmware/hermes_satellite/config.h`.

---

## Prompt for the AI

> Design the wiring schematic and a step-by-step assembly guide for an **ESP32-S3
> voice-satellite** device. It captures voice from an I²S microphone and plays
> audio replies through an I²S amplifier + speaker, with a push-to-talk button
> and a status LED. Use the exact components, pins, and power rails below. Produce:
> (1) a clean wiring diagram, (2) a bill of materials, (3) assembly steps, and
> (4) notes/warnings. Keep every connection exactly as specified.

---

## Project summary
A Wi-Fi "voice satellite": you hold the button and speak; the ESP32-S3 records
16 kHz mono audio from the INMP441 mic and sends it to a PC over Wi-Fi; the PC
replies with audio that plays back through the MAX98357A amplifier and speaker.
The LED shows status. Everything runs off USB-C 5 V.

## Main board
- **ESP32-S3 development board — MUST have PSRAM** (e.g. N8R8 or N16R8).
- Powered by **USB-C, 5 V**.

## Components (bill of materials)
| Ref | Part | Interface |
|-----|------|-----------|
| U1 | ESP32-S3 dev board (with PSRAM) | host |
| MIC1 | INMP441 I²S MEMS microphone | I²S (RX) |
| AMP1 | MAX98357A I²S class-D amplifier | I²S (TX) |
| SPK1 | Speaker, 8 Ω, 3 W | analog out |
| SW1 | Momentary push button (push-to-talk) | GPIO |
| D1 | LED, 5 mm (status) | GPIO |
| R1 | Resistor, 220 Ω (LED current limit) | — |
| — | USB-C cable + 5 V supply | power |

## Connections (wire exactly like this)

### INMP441 microphone (MIC1) → I²S bus 0
| MIC1 pin | Connect to |
|----------|-----------|
| VDD | ESP32-S3 **3V3** (3.3 V only — NOT 5 V) |
| GND | ESP32-S3 GND |
| SCK (BCLK) | ESP32-S3 **GPIO4** |
| WS (LRCL) | ESP32-S3 **GPIO5** |
| SD (data out) | ESP32-S3 **GPIO6** |
| L/R | ESP32-S3 GND (selects the left channel) |

### MAX98357A amplifier (AMP1) → I²S bus 1
| AMP1 pin | Connect to |
|----------|-----------|
| VIN | ESP32-S3 **5V** (louder; 3V3 also works) |
| GND | ESP32-S3 GND |
| BCLK | ESP32-S3 **GPIO7** |
| LRC (WS) | ESP32-S3 **GPIO15** |
| DIN | ESP32-S3 **GPIO16** |
| GAIN | leave unconnected (default 9 dB) |
| SD | leave unconnected |
| Speaker + | SPK1 + terminal |
| Speaker − | SPK1 − terminal |

### Speaker (SPK1)
- Connect across the MAX98357A **Speaker +** and **Speaker −** screw terminals.
- 8 Ω, 3 W.

### Push-to-talk button (SW1)
| SW1 leg | Connect to |
|---------|-----------|
| leg 1 | ESP32-S3 **GPIO0** |
| leg 2 | ESP32-S3 GND |
- Uses the internal pull-up; pressing pulls the pin LOW.

### Status LED (D1) with resistor (R1)
- ESP32-S3 **GPIO2** → **R1 (220 Ω)** → **D1 anode (+, long leg)**
- **D1 cathode (−, short leg)** → ESP32-S3 GND

## Power rails
- USB-C 5 V powers the board.
- **3V3 rail** → INMP441 VDD.
- **5V rail** → MAX98357A VIN.
- All grounds are common (tie every GND together).

## Notes & warnings (please include in the how-to)
1. The board **must be an ESP32-S3 with PSRAM**; enable PSRAM when flashing.
2. Two **separate I²S peripherals** are used — mic on bus 0 (GPIO4/5/6), amp on
   bus 1 (GPIO7/15/16). Do not share these pins.
3. **INMP441 VDD is 3.3 V only.** Do not connect it to 5 V.
4. **GPIO0 is a boot-strapping pin.** The button is fine here, but do **not hold
   the button while powering on / resetting** (that enters bootloader mode). If
   preferred, the button pin can be moved to another free GPIO (e.g. GPIO21) —
   update `PTT_PIN` in the firmware to match.
5. INMP441 **L/R → GND** = left channel (matches the firmware's mono-left config).
6. MAX98357A **GAIN** and **SD** pins are left floating for default behaviour.

---

## OPTIONAL — separate "custom node" board (not on the main schematic)
A second, independent board for voice-controlled relays (e.g. a fan). Only add if
you want a second diagram.
- **ESP8266 or ESP32** + **1-channel 5 V relay module**.
- Relay **IN** → board **GPIO5**; relay **VCC** → **5V**; relay **GND** → GND.
- Relay **COM/NO** switches the appliance's live wire (mains — handle safely).
- Talks to the PC over MQTT; not wired to the satellite above.
