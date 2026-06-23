#!/usr/bin/env python3
"""
LilyGo CC1101 MCP Server v2
Claude controla el dispositivo directamente como si fuera una herramienta nativa.

Instalar:
    pip install mcp websockets anthropic

Configurar en Claude Desktop (~/.claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "lilygo": {
          "command": "python",
          "args": ["/ruta/a/lilygo_mcp.py"],
          "env": { "LILYGO_HOST": "192.168.1.XX" }
        }
      }
    }
"""

import asyncio
import json
import os
import logging
import time
from typing import Any
from datetime import datetime
from pathlib import Path

import websockets
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

log = logging.getLogger("lilygo-mcp")
logging.basicConfig(level=logging.INFO)

LILYGO_HOST = os.getenv("LILYGO_HOST", "192.168.1.100")
LILYGO_PORT = int(os.getenv("LILYGO_PORT", "8766"))
LILYGO_URI  = f"ws://{LILYGO_HOST}:{LILYGO_PORT}"

app = Server("lilygo-cc1101")
_ws: websockets.WebSocketClientProtocol | None = None
_session_captures: list[dict] = []


async def ws_connect() -> websockets.WebSocketClientProtocol:
    global _ws
    if _ws is None or _ws.closed:
        _ws = await websockets.connect(LILYGO_URI, open_timeout=6)
    return _ws


async def device(payload: dict, timeout: float = 12.0) -> dict:
    ws = await ws_connect()
    await ws.send(json.dumps(payload))
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    result = json.loads(raw)
    # Keep capture history for session context
    if payload.get("cmd") in ("rf_scan", "nfc_read", "wifi_scan"):
        _session_captures.append({"ts": datetime.now().isoformat(), "type": payload["cmd"], "result": result})
    return result


# ─── Tools ────────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [

        # ── Status & info ────────────────────────────────────────────────────
        types.Tool(
            name="device_status",
            description=(
                "Ping the LilyGo and get full device info: WiFi IP, battery level, "
                "firmware version, CC1101 chip status, free heap, SD card space, "
                "and what app is currently running on screen."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="session_summary",
            description=(
                "Get a summary of everything captured or done in this session: "
                "list of RF signals captured, NFC cards read, WiFi scans performed, "
                "commands sent. Useful to understand what has been collected so far."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # ── RF / Sub-GHz ─────────────────────────────────────────────────────
        types.Tool(
            name="rf_scan",
            description=(
                "Scan for RF signals. The device listens on the specified frequency "
                "(or auto-scans 300-928 MHz) and returns the captured signal as a "
                "Flipper Zero .sub file. Use this BEFORE analyzing an RF signal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "frequency_mhz": {"type": "number", "description": "Frequency in MHz (e.g. 433.92). Use 0 for auto-scan."},
                    "modulation":    {"type": "string", "enum": ["OOK", "FSK", "ASK", "auto"], "description": "Signal modulation type"},
                    "timeout_secs":  {"type": "integer", "description": "Max seconds to wait for a signal (default 20)"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="rf_get_last",
            description="Get the most recently captured RF signal from the SD card (.sub file content).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="rf_transmit",
            description=(
                "Replay/transmit an RF signal from the device. "
                "Provide either a .sub file content string or a raw signal. "
                "Use only on your own devices."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sub_content":    {"type": "string", "description": "Flipper Zero .sub file content to transmit"},
                    "frequency_mhz":  {"type": "number", "description": "Override frequency in MHz"},
                    "repeat":         {"type": "integer", "description": "How many times to repeat transmission (default 3)"},
                },
                "required": ["sub_content"],
            },
        ),
        types.Tool(
            name="rf_bruteforce",
            description=(
                "Send a sequence of RF codes to a target device (garage door, gate, alarm). "
                "Claude generates smart codes prioritizing manufacturer defaults. "
                "USE ONLY ON YOUR OWN DEVICES."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "protocol":      {"type": "string", "description": "Protocol: Princeton, CAME, Nice_Flo, Keeloq, Linear, etc."},
                    "codes":         {"type": "array", "items": {"type": "string"}, "description": "Hex codes to try"},
                    "frequency_mhz": {"type": "number", "description": "Frequency in MHz (default 433.92)"},
                    "delay_ms":      {"type": "integer", "description": "Delay between codes in ms (default 400)"},
                    "stop_on_response": {"type": "boolean", "description": "Stop when device detects a response (default true)"},
                },
                "required": ["codes"],
            },
        ),
        types.Tool(
            name="rf_spectrum",
            description=(
                "Show RF spectrum activity across a frequency range. "
                "Returns signal strength at each frequency — useful to find what frequencies are active nearby."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start_mhz": {"type": "number", "description": "Start frequency in MHz (default 300)"},
                    "end_mhz":   {"type": "number", "description": "End frequency in MHz (default 928)"},
                    "step_mhz":  {"type": "number", "description": "Step size in MHz (default 1)"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="rf_save",
            description="Save a captured RF signal to the device SD card with a custom name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "File name (without .sub extension)"},
                    "sub_content": {"type": "string", "description": "Signal content to save"},
                },
                "required": ["name", "sub_content"],
            },
        ),

        # ── NFC ──────────────────────────────────────────────────────────────
        types.Tool(
            name="nfc_read",
            description=(
                "Read an NFC card placed near the device RIGHT NOW. "
                "Returns card type, UID, raw data, and sector contents if readable. "
                "Supports: Mifare Classic, NTAG, EMV bank cards, FeliCa, ISO15693."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout_secs": {"type": "integer", "description": "Seconds to wait for card (default 15)"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="nfc_get_last",
            description="Get the most recent NFC dump from SD card.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="nfc_emulate",
            description=(
                "Emulate an NFC card from a saved dump. "
                "The device will appear as that card to NFC readers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "nfc_content": {"type": "string", "description": "NFC dump file content (.nfc format)"},
                    "duration_secs": {"type": "integer", "description": "How long to emulate (default 30)"},
                },
                "required": ["nfc_content"],
            },
        ),
        types.Tool(
            name="nfc_dictionary_attack",
            description=(
                "Run a dictionary attack on a Mifare Classic card using common keys. "
                "Returns which sectors were unlocked and their contents."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout_secs": {"type": "integer", "description": "Max time for attack (default 60)"},
                },
                "required": [],
            },
        ),

        # ── WiFi ─────────────────────────────────────────────────────────────
        types.Tool(
            name="wifi_scan",
            description=(
                "Scan for nearby WiFi networks. Returns SSID, BSSID, channel, "
                "signal strength, encryption type, WPS status for all visible APs."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="wifi_capture_handshake",
            description=(
                "Capture a WPA2 handshake from a target AP by sending deauth frames "
                "and waiting for a client to reconnect. For authorized auditing only. "
                "Returns the .pcap handshake file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "bssid":        {"type": "string", "description": "Target AP MAC address"},
                    "channel":      {"type": "integer", "description": "WiFi channel"},
                    "timeout_secs": {"type": "integer", "description": "Seconds to wait for handshake (default 30)"},
                },
                "required": ["bssid", "channel"],
            },
        ),
        types.Tool(
            name="wifi_deauth",
            description=(
                "Send WiFi deauthentication frames to disconnect clients from an AP. "
                "Authorized testing only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "bssid":   {"type": "string",  "description": "Target AP BSSID"},
                    "channel": {"type": "integer", "description": "WiFi channel"},
                    "count":   {"type": "integer", "description": "Number of deauth frames (default 100)"},
                    "client":  {"type": "string",  "description": "Target specific client MAC (optional, default FF:FF:FF:FF:FF:FF)"},
                },
                "required": ["bssid", "channel"],
            },
        ),
        types.Tool(
            name="wifi_evil_portal",
            description=(
                "Launch a captive portal that mimics a target WiFi network to capture credentials. "
                "Creates a fake AP with the same SSID. Authorized testing only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ssid":    {"type": "string", "description": "SSID to clone"},
                    "channel": {"type": "integer", "description": "Channel to broadcast on"},
                    "duration_secs": {"type": "integer", "description": "How long to run (default 60)"},
                },
                "required": ["ssid"],
            },
        ),

        # ── Bluetooth / BLE ──────────────────────────────────────────────────
        types.Tool(
            name="ble_scan",
            description=(
                "Scan for nearby Bluetooth Low Energy devices. "
                "Returns device name, MAC, RSSI, manufacturer data, services."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "duration_secs": {"type": "integer", "description": "Scan duration (default 10)"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="ble_spam",
            description=(
                "Send BLE advertisement spam (Apple/Android/Windows pairing popups). "
                "For testing notification systems."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["apple", "android", "windows", "all"], "description": "Target device type"},
                    "duration_secs": {"type": "integer", "description": "Duration in seconds (default 10)"},
                },
                "required": [],
            },
        ),

        # ── Infrared ─────────────────────────────────────────────────────────
        types.Tool(
            name="ir_capture",
            description="Capture an infrared signal (TV remote, AC, etc). Point remote at device and press button.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout_secs": {"type": "integer", "description": "Seconds to wait (default 15)"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="ir_transmit",
            description="Transmit a saved infrared signal. Can control TVs, ACs, and other IR devices.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ir_content": {"type": "string", "description": "IR file content (.ir format)"},
                    "repeat":     {"type": "integer", "description": "Repeat count (default 1)"},
                },
                "required": ["ir_content"],
            },
        ),
        types.Tool(
            name="ir_universal_remote",
            description=(
                "Try to control a device (TV, projector, AC) by sending common IR codes "
                "for a given brand. Useful when you don't have the original remote."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device_type": {"type": "string", "enum": ["tv", "ac", "projector", "fan"], "description": "Device to control"},
                    "brand":       {"type": "string", "description": "Brand name (Samsung, LG, Sony, Philips, etc). Use 'unknown' to try all."},
                    "command":     {"type": "string", "enum": ["power", "volume_up", "volume_down", "mute", "channel_up", "channel_down"], "description": "Command to send"},
                },
                "required": ["device_type", "command"],
            },
        ),

        # ── File system ──────────────────────────────────────────────────────
        types.Tool(
            name="files_list",
            description="List files on the device SD card.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "Directory path (default /ext). Options: /ext/subghz, /ext/nfc, /ext/infrared, /ext/wifi"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="files_read",
            description="Read a specific file from the device SD card.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path on device"},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="files_delete",
            description="Delete a file from the device SD card.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path on device"},
                },
                "required": ["path"],
            },
        ),
    ]


# ─── Handlers ─────────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
    except ConnectionError as e:
        return [types.TextContent(type="text", text=f"ERROR: No se puede conectar al LilyGo en {LILYGO_URI}\n{e}\n\nAsegúrate de que el dispositivo está en WiFi y la app AI Agent está abierta.")]
    except asyncio.TimeoutError:
        return [types.TextContent(type="text", text="ERROR: El dispositivo no respondió a tiempo. ¿Está ocupado escaneando?")]
    except Exception as e:
        log.error(f"Tool {name} error: {e}", exc_info=True)
        return [types.TextContent(type="text", text=f"ERROR: {e}")]


async def _dispatch(name: str, args: dict) -> dict:

    if name == "device_status":
        return await device({"cmd": "ping"}, timeout=5)

    elif name == "session_summary":
        return {
            "captures_this_session": len(_session_captures),
            "history": _session_captures[-20:],
            "device": LILYGO_URI,
        }

    # RF
    elif name == "rf_scan":
        freq = args.get("frequency_mhz", 0)
        t    = args.get("timeout_secs", 20)
        return await device({"cmd": "rf_scan", "frequency_mhz": freq,
                              "modulation": args.get("modulation", "auto"),
                              "timeout_secs": t}, timeout=t + 5)

    elif name == "rf_get_last":
        return await device({"cmd": "get_file", "dir": "/ext/subghz", "newest": True})

    elif name == "rf_transmit":
        return await device({"cmd": "rf_transmit",
                              "sub_content": args["sub_content"],
                              "frequency_mhz": args.get("frequency_mhz", 433.92),
                              "repeat": args.get("repeat", 3)}, timeout=20)

    elif name == "rf_bruteforce":
        codes   = args["codes"]
        delay   = args.get("delay_ms", 400)
        timeout = len(codes) * (delay / 1000) + 15
        return await device({"cmd": "rf_bruteforce",
                              "protocol": args.get("protocol", "OOK"),
                              "codes": codes,
                              "frequency_mhz": args.get("frequency_mhz", 433.92),
                              "delay_ms": delay,
                              "stop_on_response": args.get("stop_on_response", True)},
                             timeout=timeout)

    elif name == "rf_spectrum":
        return await device({"cmd": "rf_spectrum",
                              "start_mhz": args.get("start_mhz", 300),
                              "end_mhz":   args.get("end_mhz", 928),
                              "step_mhz":  args.get("step_mhz", 1)}, timeout=60)

    elif name == "rf_save":
        return await device({"cmd": "save_file",
                              "path": f"/ext/subghz/{args['name']}.sub",
                              "content": args["sub_content"]})

    # NFC
    elif name == "nfc_read":
        t = args.get("timeout_secs", 15)
        return await device({"cmd": "nfc_read", "timeout_secs": t}, timeout=t + 5)

    elif name == "nfc_get_last":
        return await device({"cmd": "get_file", "dir": "/ext/nfc", "newest": True})

    elif name == "nfc_emulate":
        t = args.get("duration_secs", 30)
        return await device({"cmd": "nfc_emulate",
                              "nfc_content": args["nfc_content"],
                              "duration_secs": t}, timeout=t + 5)

    elif name == "nfc_dictionary_attack":
        t = args.get("timeout_secs", 60)
        return await device({"cmd": "nfc_dict_attack", "timeout_secs": t}, timeout=t + 5)

    # WiFi
    elif name == "wifi_scan":
        return await device({"cmd": "wifi_scan"}, timeout=15)

    elif name == "wifi_capture_handshake":
        t = args.get("timeout_secs", 30)
        return await device({"cmd": "wifi_handshake",
                              "bssid": args["bssid"],
                              "channel": args["channel"],
                              "timeout_secs": t}, timeout=t + 10)

    elif name == "wifi_deauth":
        return await device({"cmd": "wifi_deauth",
                              "bssid":   args["bssid"],
                              "channel": args["channel"],
                              "count":   args.get("count", 100),
                              "client":  args.get("client", "FF:FF:FF:FF:FF:FF")}, timeout=30)

    elif name == "wifi_evil_portal":
        t = args.get("duration_secs", 60)
        return await device({"cmd": "wifi_evil_portal",
                              "ssid": args["ssid"],
                              "channel": args.get("channel", 6),
                              "duration_secs": t}, timeout=t + 5)

    # BLE
    elif name == "ble_scan":
        t = args.get("duration_secs", 10)
        return await device({"cmd": "ble_scan", "duration_secs": t}, timeout=t + 5)

    elif name == "ble_spam":
        t = args.get("duration_secs", 10)
        return await device({"cmd": "ble_spam",
                              "target": args.get("target", "all"),
                              "duration_secs": t}, timeout=t + 5)

    # IR
    elif name == "ir_capture":
        t = args.get("timeout_secs", 15)
        return await device({"cmd": "ir_capture", "timeout_secs": t}, timeout=t + 5)

    elif name == "ir_transmit":
        return await device({"cmd": "ir_transmit",
                              "ir_content": args["ir_content"],
                              "repeat": args.get("repeat", 1)}, timeout=10)

    elif name == "ir_universal_remote":
        return await device({"cmd": "ir_universal",
                              "device_type": args["device_type"],
                              "brand":       args.get("brand", "unknown"),
                              "command":     args["command"]}, timeout=15)

    # Files
    elif name == "files_list":
        return await device({"cmd": "list_files", "dir": args.get("dir", "/ext")})

    elif name == "files_read":
        return await device({"cmd": "get_file", "path": args["path"]})

    elif name == "files_delete":
        return await device({"cmd": "delete_file", "path": args["path"]})

    else:
        return {"error": f"Herramienta desconocida: {name}"}


async def main():
    log.info(f"LilyGo MCP v2 — dispositivo en {LILYGO_URI}")
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
