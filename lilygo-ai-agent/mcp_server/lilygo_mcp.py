#!/usr/bin/env python3
"""
LilyGo CC1101 MCP Server

Exposes the LilyGo device as MCP tools so Claude can:
- Receive and analyze RF/WiFi/NFC captures directly
- Send commands to the device
- Generate bruteforce sequences
- Read captured files from the SD card

Install:
    pip install mcp websockets

Run:
    python lilygo_mcp.py

Configure in Claude Desktop (~/.claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "lilygo": {
          "command": "python",
          "args": ["/path/to/lilygo_mcp.py"],
          "env": {
            "LILYGO_HOST": "192.168.1.XX",
            "LILYGO_PORT": "8766"
          }
        }
      }
    }

Configure in Claude Code (.claude/settings.json):
    {
      "mcpServers": {
        "lilygo": {
          "command": "python",
          "args": ["/path/to/lilygo_mcp.py"]
        }
      }
    }
"""

import asyncio
import json
import os
import logging
from typing import Any
from datetime import datetime
from pathlib import Path

import websockets
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lilygo-mcp")

LILYGO_HOST = os.getenv("LILYGO_HOST", "192.168.1.100")
LILYGO_PORT = int(os.getenv("LILYGO_PORT", "8766"))
LILYGO_URI  = f"ws://{LILYGO_HOST}:{LILYGO_PORT}"

app = Server("lilygo-cc1101")

_ws: websockets.WebSocketClientProtocol | None = None


async def get_ws() -> websockets.WebSocketClientProtocol:
    """Get or create WebSocket connection to the device."""
    global _ws
    if _ws is None or _ws.closed:
        try:
            _ws = await websockets.connect(LILYGO_URI, open_timeout=5)
            log.info(f"Connected to LilyGo at {LILYGO_URI}")
        except Exception as e:
            raise ConnectionError(f"Cannot connect to LilyGo at {LILYGO_URI}: {e}")
    return _ws


async def send_and_recv(payload: dict, timeout: float = 10.0) -> dict:
    """Send JSON to device and wait for response."""
    ws = await get_ws()
    await ws.send(json.dumps(payload))
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


# ── Tool definitions ──────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="device_status",
            description="Check if LilyGo is connected and get device info (battery, WiFi, firmware version).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_last_rf_capture",
            description="Get the most recent Sub-GHz RF capture from the device SD card. Returns raw .sub file content for analysis.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_last_nfc_dump",
            description="Get the most recent NFC card dump from the device SD card.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_wifi_scan",
            description="Get the most recent WiFi scan results from the device.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="send_rf_signal",
            description="Transmit an RF signal from the device. Provide the .sub file content to replay.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sub_content": {
                        "type": "string",
                        "description": "Content of a Flipper Zero .sub file to transmit"
                    },
                    "frequency": {
                        "type": "number",
                        "description": "Frequency in Hz (e.g. 433920000 for 433.92 MHz)"
                    }
                },
                "required": ["sub_content"]
            },
        ),
        types.Tool(
            name="start_rf_scan",
            description="Start scanning for RF signals on a given frequency. Returns after capturing a signal or timeout.",
            inputSchema={
                "type": "object",
                "properties": {
                    "frequency": {
                        "type": "number",
                        "description": "Frequency in Hz. Use 0 for auto-scan across common frequencies."
                    },
                    "timeout_secs": {
                        "type": "integer",
                        "description": "How many seconds to scan before giving up (default 15)"
                    }
                },
                "required": []
            },
        ),
        types.Tool(
            name="bruteforce_rf",
            description="Send a sequence of RF codes to brute-force a device (garage door, etc). Use only on your own devices.",
            inputSchema={
                "type": "object",
                "properties": {
                    "protocol": {
                        "type": "string",
                        "description": "Protocol name (e.g. Princeton, CAME, Nice_Flo)"
                    },
                    "codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Array of hex codes to try"
                    },
                    "frequency": {
                        "type": "number",
                        "description": "Frequency in Hz (default 433920000)"
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": "Delay between codes in milliseconds (default 500)"
                    }
                },
                "required": ["codes"]
            },
        ),
        types.Tool(
            name="read_nfc_card",
            description="Read an NFC card placed on the device right now. Hold card near device before calling.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="list_saved_files",
            description="List files saved on the device SD card, filtered by type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_type": {
                        "type": "string",
                        "enum": ["rf", "nfc", "wifi", "ir", "all"],
                        "description": "Type of files to list"
                    }
                },
                "required": []
            },
        ),
        types.Tool(
            name="get_saved_file",
            description="Get the contents of a specific file from the device SD card.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path on device (e.g. /ext/subghz/capture1.sub)"
                    }
                },
                "required": ["path"]
            },
        ),
        types.Tool(
            name="run_wifi_deauth",
            description="Send WiFi deauth packets to a target AP. For authorized testing only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bssid": {
                        "type": "string",
                        "description": "Target AP BSSID (MAC address)"
                    },
                    "channel": {
                        "type": "integer",
                        "description": "WiFi channel"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of deauth frames to send (default 100)"
                    }
                },
                "required": ["bssid", "channel"]
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except ConnectionError as e:
        return [types.TextContent(type="text", text=f"ERROR: {e}\nMake sure LilyGo is on WiFi and AI Agent app is running.")]
    except asyncio.TimeoutError:
        return [types.TextContent(type="text", text="ERROR: Device did not respond in time. Is it scanning/busy?")]
    except Exception as e:
        log.error(f"Tool {name} error: {e}", exc_info=True)
        return [types.TextContent(type="text", text=f"ERROR: {e}")]


async def _dispatch(name: str, args: dict) -> dict:

    if name == "device_status":
        return await send_and_recv({"type": "command", "cmd": "ping"})

    elif name == "get_last_rf_capture":
        return await send_and_recv({
            "type": "command",
            "cmd": "get_file",
            "dir": "/ext/subghz",
            "newest": True
        }, timeout=8)

    elif name == "get_last_nfc_dump":
        return await send_and_recv({
            "type": "command",
            "cmd": "get_file",
            "dir": "/ext/nfc",
            "newest": True
        }, timeout=8)

    elif name == "get_wifi_scan":
        return await send_and_recv({
            "type": "command",
            "cmd": "get_file",
            "path": "/ext/wifi/last_scan.json"
        }, timeout=8)

    elif name == "send_rf_signal":
        return await send_and_recv({
            "type": "command",
            "cmd": "rf_transmit",
            "sub_content": args["sub_content"],
            "frequency": args.get("frequency", 433920000)
        }, timeout=15)

    elif name == "start_rf_scan":
        timeout = args.get("timeout_secs", 15)
        return await send_and_recv({
            "type": "command",
            "cmd": "rf_scan",
            "frequency": args.get("frequency", 0),
            "timeout_secs": timeout
        }, timeout=timeout + 5)

    elif name == "bruteforce_rf":
        return await send_and_recv({
            "type": "command",
            "cmd": "rf_bruteforce",
            "protocol": args.get("protocol", "OOK"),
            "codes": args["codes"],
            "frequency": args.get("frequency", 433920000),
            "delay_ms": args.get("delay_ms", 500)
        }, timeout=len(args["codes"]) * (args.get("delay_ms", 500) / 1000) + 10)

    elif name == "read_nfc_card":
        return await send_and_recv({
            "type": "command",
            "cmd": "nfc_read"
        }, timeout=20)

    elif name == "list_saved_files":
        file_type = args.get("file_type", "all")
        dir_map = {
            "rf": "/ext/subghz",
            "nfc": "/ext/nfc",
            "wifi": "/ext/wifi",
            "ir": "/ext/infrared",
            "all": "/ext"
        }
        return await send_and_recv({
            "type": "command",
            "cmd": "list_files",
            "dir": dir_map.get(file_type, "/ext")
        }, timeout=8)

    elif name == "get_saved_file":
        return await send_and_recv({
            "type": "command",
            "cmd": "get_file",
            "path": args["path"]
        }, timeout=8)

    elif name == "run_wifi_deauth":
        return await send_and_recv({
            "type": "command",
            "cmd": "wifi_deauth",
            "bssid": args["bssid"],
            "channel": args["channel"],
            "count": args.get("count", 100)
        }, timeout=30)

    else:
        return {"error": f"Unknown tool: {name}"}


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    log.info(f"LilyGo MCP Server starting — device at {LILYGO_URI}")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
