// ----------------------------------------------------------------------------
//  Hermes satellite shared settings.
//  Copy this file to  config.h , edit it, then flash.
// ----------------------------------------------------------------------------
#pragma once

// -- Bridge (the PC running hermes_bridge.py) ---------------------------------
// Leave USE_MDNS = 1 and the satellite finds your PC automatically by name.
// BRIDGE_HOST is only the FALLBACK if auto-discovery fails — set it to the IP
// the bridge prints on startup (e.g. "192.168.1.50").
#define USE_MDNS       1
#define BRIDGE_HOST    "192.168.1.50"
#define BRIDGE_PORT    8787
#define BRIDGE_PATH    "/voice"

// Must match HERMES_TOKEN in bridge/.env (leave "" for LAN-only, no token).
#define BRIDGE_TOKEN   "CHANGE_ME_TO_MATCH_HERMES_TOKEN"

// -- Behaviour ---------------------------------------------------------------
#define MAX_RECORD_SECONDS   3      // classic ESP32 RAM-safe default
#define MIC_GAIN_SHIFT       16     // 32->16 bit shift. LOWER = louder (try 11-13 if quiet)
#define BRIDGE_TIMEOUT_MS    30000  // time to allow for STT + LLM + web search + TTS

// -- ESP32-S3 pins from the schematic. The classic ESP32 sketch OVERRIDES these
//    in hermes_satellite.ino with its safe GPIO map (mic D14/D27/D26, amp
//    D25/D33/D32). Only edit these if you build the ESP32-S3 wiring option.
#define MIC_BCLK_PIN     4
#define MIC_WS_PIN       5
#define MIC_DATA_PIN     6
#define AMP_BCLK_PIN     7
#define AMP_WS_PIN       15
#define AMP_DATA_PIN     16
#define PTT_PIN          0
#define STATUS_LED_PIN   2

// -- Audio format (must stay 16 kHz / 16-bit / mono for Whisper) --------------
#define SAMPLE_RATE      16000
#define BITS_PER_SAMPLE  16
#define NUM_CHANNELS     1
