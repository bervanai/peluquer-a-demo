#!/usr/bin/env python3
"""
LilyGo CC1101 AI Agent Server
Runs on your PC - connects to the device via WebSocket and uses Claude AI
to analyze captured signals and suggest next steps.

Usage:
    pip install -r requirements.txt
    python server.py

Then on your device go to: Apps > AI Agent > Connect
"""

import asyncio
import json
import logging
import os
import base64
import struct
from datetime import datetime
from typing import Optional
from pathlib import Path

import anthropic
import websockets
from websockets.server import serve
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ai-agent")
console = Console()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8765"))

SYSTEM_PROMPT = """You are an expert RF security researcher and embedded systems specialist
analyzing data captured by a LilyGo T-Embed CC1101 running Flipper Zero ESP32 Port firmware.

Your role:
1. Analyze raw RF captures, WiFi scans, NFC dumps, and signal data
2. Identify protocols, devices, and potential vulnerabilities
3. Suggest concrete next steps for the researcher
4. Explain findings in clear, actionable language
5. Generate replay payloads or attack vectors when asked (for authorized testing only)

Response format for device display (keep concise, max 200 chars for on-device summary):
Always include:
- PROTOCOL: identified protocol
- DEVICE: likely device type
- ACTION: recommended next step
- RISK: LOW/MEDIUM/HIGH

For PC display you can be more detailed."""

client: Optional[anthropic.Anthropic] = None
session_log = Path("sessions") / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"


def init_anthropic():
    global client
    if not ANTHROPIC_API_KEY:
        console.print("[red]ERROR: ANTHROPIC_API_KEY not set in .env[/red]")
        return False
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    console.print("[green]Claude AI ready[/green]")
    return True


def log_session(event_type: str, data: dict):
    session_log.parent.mkdir(exist_ok=True)
    with open(session_log, "a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(), "type": event_type, **data}) + "\n")


async def analyze_with_claude(payload: dict) -> dict:
    """Send captured data to Claude and get analysis + recommendations."""
    if not client:
        return {"error": "Claude not initialized", "summary": "AI offline"}

    data_type = payload.get("type", "unknown")
    raw_data = payload.get("data", {})

    user_message = f"""Analyze this {data_type} capture from a LilyGo CC1101 device:

{json.dumps(raw_data, indent=2)}

Provide:
1. Protocol identification
2. Device/system type
3. Security assessment
4. Recommended actions (for authorized testing)
5. A SHORT summary (max 180 chars) for the device display"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )

        full_analysis = response.content[0].text

        lines = full_analysis.split("\n")
        summary_line = next(
            (l for l in lines if "summary" in l.lower() or l.startswith("PROTOCOL") or len(l) < 180),
            full_analysis[:180]
        )

        console.print(Panel(full_analysis, title=f"[cyan]Claude Analysis - {data_type}[/cyan]"))

        return {
            "analysis": full_analysis,
            "summary": summary_line.strip()[:180],
            "model": "claude-sonnet-4-6",
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens
        }

    except anthropic.APIError as e:
        log.error(f"Claude API error: {e}")
        return {"error": str(e), "summary": f"AI error: {str(e)[:100]}"}


async def handle_command(payload: dict) -> dict:
    """Handle direct commands from the device."""
    cmd = payload.get("cmd", "")

    if cmd == "ping":
        return {"status": "ok", "message": "AI Agent online", "model": "claude-sonnet-4-6"}

    elif cmd == "generate_bruteforce":
        protocol = payload.get("protocol", "unknown")
        bits = payload.get("bits", 24)
        return await generate_bruteforce_sequence(protocol, bits)

    elif cmd == "decode_signal":
        return await analyze_with_claude({
            "type": "rf_signal",
            "data": payload.get("signal", {})
        })

    elif cmd == "suggest_attack":
        target = payload.get("target_info", {})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"For authorized testing, suggest attack vectors for: {json.dumps(target)}"
            }]
        )
        return {"suggestion": response.content[0].text, "summary": response.content[0].text[:180]}

    elif cmd == "save_capture":
        return save_capture(payload)

    elif cmd == "list_captures":
        return list_captures()

    return {"error": f"Unknown command: {cmd}"}


async def generate_bruteforce_sequence(protocol: str, bits: int) -> dict:
    """Use Claude to generate intelligent bruteforce sequences."""
    prompt = f"""Generate a smart bruteforce sequence for {protocol} protocol with {bits}-bit codes.

    Instead of sequential, use:
    1. Common default codes first
    2. Manufacturer default patterns
    3. Then systematic sweep

    Return as JSON with 'sequences' array of hex strings, max 50 entries to start.
    Focus on most likely codes first."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1:
            data = json.loads(text[start:end])
            return {"sequences": data.get("sequences", []), "summary": f"Generated {len(data.get('sequences', []))} smart codes"}
    except json.JSONDecodeError:
        pass

    return {"sequences": [], "summary": "Bruteforce generation failed", "raw": text}


def save_capture(payload: dict) -> dict:
    captures_dir = Path("captures")
    captures_dir.mkdir(exist_ok=True)

    filename = f"{payload.get('type', 'unknown')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = captures_dir / filename

    with open(filepath, "w") as f:
        json.dump(payload, f, indent=2)

    console.print(f"[green]Capture saved: {filepath}[/green]")
    return {"status": "saved", "file": str(filepath), "summary": f"Saved to {filename}"}


def list_captures() -> dict:
    captures_dir = Path("captures")
    if not captures_dir.exists():
        return {"captures": [], "summary": "No captures yet"}

    files = sorted(captures_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    captures = [{"name": f.name, "size": f.stat().st_size, "ts": f.stat().st_mtime} for f in files[:20]]

    table = Table(title="Saved Captures")
    table.add_column("File", style="cyan")
    table.add_column("Size", justify="right")
    for c in captures:
        table.add_row(c["name"], f"{c['size']}B")
    console.print(table)

    return {"captures": captures, "summary": f"{len(captures)} captures stored"}


async def handle_client(websocket):
    client_addr = websocket.remote_address
    console.print(f"\n[bold green]Device connected: {client_addr}[/bold green]")

    try:
        await websocket.send(json.dumps({
            "type": "welcome",
            "message": "LilyGo AI Agent online",
            "model": "claude-sonnet-4-6",
            "capabilities": ["rf_analysis", "wifi_analysis", "nfc_analysis", "bruteforce_gen", "protocol_decode"]
        }))

        async for raw_message in websocket:
            try:
                payload = json.loads(raw_message)
                msg_type = payload.get("type", "unknown")

                log.info(f"Received: {msg_type} from {client_addr}")
                log_session("received", payload)

                console.print(f"\n[yellow]>> {msg_type}[/yellow]", payload.get("summary", ""))

                if msg_type == "command":
                    result = await handle_command(payload)
                elif msg_type in ("rf_capture", "wifi_scan", "nfc_dump", "ir_capture", "signal_raw"):
                    result = await analyze_with_claude(payload)
                elif msg_type == "ping":
                    result = {"status": "ok", "ts": datetime.now().isoformat()}
                else:
                    result = {"error": f"Unknown message type: {msg_type}"}

                result["req_type"] = msg_type
                log_session("sent", result)
                await websocket.send(json.dumps(result))

            except json.JSONDecodeError:
                await websocket.send(json.dumps({"error": "Invalid JSON"}))
            except Exception as e:
                log.error(f"Handler error: {e}", exc_info=True)
                await websocket.send(json.dumps({"error": str(e)}))

    except websockets.exceptions.ConnectionClosed:
        console.print(f"[red]Device disconnected: {client_addr}[/red]")


async def main():
    console.print(Panel.fit(
        "[bold cyan]LilyGo CC1101 AI Agent Server[/bold cyan]\n"
        "Powered by Claude AI\n\n"
        f"Listening on [bold]{SERVER_HOST}:{SERVER_PORT}[/bold]\n"
        "Configure your device: Apps > AI Agent > Server IP",
        border_style="cyan"
    ))

    if not init_anthropic():
        return

    async with serve(handle_client, SERVER_HOST, SERVER_PORT):
        console.print(f"[green]Server ready. Waiting for device...[/green]")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
