#!/usr/bin/env python3
"""
LilyGo CC1101 Control Center
GUI de escritorio para controlar el dispositivo y ver análisis de Claude AI.

Instalar:
    pip install customtkinter websockets anthropic

Ejecutar:
    python gui.py
"""

import asyncio
import json
import os
import sys
import threading
import time
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import anthropic
import websockets

# ─── Tema ─────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

DARK_BG     = "#0d1117"
PANEL_BG    = "#161b22"
BORDER      = "#30363d"
ACCENT      = "#58a6ff"
ACCENT2     = "#f78166"
SUCCESS     = "#3fb950"
WARNING     = "#d29922"
TEXT        = "#e6edf3"
TEXT_DIM    = "#8b949e"
RF_COLOR    = "#58a6ff"
NFC_COLOR   = "#bc8cff"
WIFI_COLOR  = "#79c0ff"
BLE_COLOR   = "#56d364"
IR_COLOR    = "#ffa657"

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─── State ────────────────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.device_ip = "192.168.1.100"
        self.device_port = 8766
        self.ai_client: Optional[anthropic.Anthropic] = None
        self.captures: list[dict] = []
        self.event_queue: queue.Queue = queue.Queue()
        self.loop: Optional[asyncio.AbstractEventLoop] = None


state = AppState()


# ─── Async helpers (run in background thread) ─────────────────────────────────

def start_event_loop():
    state.loop = asyncio.new_event_loop()
    asyncio.set_event_loop(state.loop)
    state.loop.run_forever()


def run_async(coro):
    if state.loop:
        return asyncio.run_coroutine_threadsafe(coro, state.loop)


async def ws_connect(ip: str, port: int) -> bool:
    try:
        uri = f"ws://{ip}:{port}"
        state.ws = await asyncio.wait_for(websockets.connect(uri), timeout=5)
        state.connected = True
        state.device_ip = ip
        state.event_queue.put(("connected", {"ip": ip, "port": port}))
        asyncio.ensure_future(ws_receive_loop())
        return True
    except Exception as e:
        state.connected = False
        state.event_queue.put(("error", {"msg": f"No se pudo conectar: {e}"}))
        return False


async def ws_receive_loop():
    try:
        async for raw in state.ws:
            try:
                data = json.loads(raw)
                state.event_queue.put(("device_data", data))
            except json.JSONDecodeError:
                pass
    except Exception:
        state.connected = False
        state.event_queue.put(("disconnected", {}))


async def ws_send(payload: dict, timeout: float = 15.0) -> Optional[dict]:
    if not state.ws or not state.connected:
        return None
    await state.ws.send(json.dumps(payload))
    # Response comes via ws_receive_loop → event_queue
    return {"sent": True}


async def ws_disconnect():
    if state.ws:
        await state.ws.close()
    state.connected = False
    state.ws = None


def send_command(cmd: dict):
    run_async(ws_send(cmd))


# ─── Claude AI ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert RF/NFC/WiFi security researcher analyzing data
from a LilyGo CC1101 device running Flipper Zero ESP32 Port firmware.

Analyze captures and provide:
- PROTOCOL: identified protocol or technology
- DEVICE: likely device type
- VULNERABILITY: any security issues found
- ACTION: recommended next step
- RISK: LOW / MEDIUM / HIGH

Be concise and actionable. Use bullet points."""


def analyze_with_claude(data: dict, data_type: str, callback):
    if not ANTHROPIC_KEY:
        callback("⚠️ Configura ANTHROPIC_API_KEY en el panel de ajustes.")
        return

    def _run():
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            msg = f"Analyze this {data_type} capture:\n\n{json.dumps(data, indent=2)}"
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": msg}]
            )
            callback(response.content[0].text)
        except Exception as e:
            callback(f"Error Claude API: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ─── Componentes UI ───────────────────────────────────────────────────────────

class StatusBar(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color=PANEL_BG, corner_radius=0, height=32)
        self.pack(fill="x", side="bottom")

        self.dot = ctk.CTkLabel(self, text="●", text_color="#f85149", font=("Consolas", 14))
        self.dot.pack(side="left", padx=(12, 4))

        self.status_lbl = ctk.CTkLabel(self, text="Desconectado", text_color=TEXT_DIM,
                                        font=("Consolas", 11))
        self.status_lbl.pack(side="left")

        self.info_lbl = ctk.CTkLabel(self, text="", text_color=TEXT_DIM, font=("Consolas", 11))
        self.info_lbl.pack(side="right", padx=12)

    def set_connected(self, ip: str):
        self.dot.configure(text_color=SUCCESS)
        self.status_lbl.configure(text=f"Conectado  —  {ip}:8766", text_color=SUCCESS)

    def set_disconnected(self):
        self.dot.configure(text_color="#f85149")
        self.status_lbl.configure(text="Desconectado", text_color=TEXT_DIM)

    def set_info(self, text: str):
        self.info_lbl.configure(text=text)


class TerminalBox(ctk.CTkTextbox):
    """Área de salida estilo terminal."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, font=("Consolas", 11), text_color=TEXT,
                          fg_color=DARK_BG, corner_radius=6,
                          wrap="word", state="disabled", **kwargs)

    def write(self, text: str, color: str = TEXT, prefix: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {prefix}{text}\n"
        self.configure(state="normal")
        self.insert("end", line)
        self.configure(state="disabled")
        self.see("end")

    def write_json(self, data: dict):
        self.write(json.dumps(data, indent=2, ensure_ascii=False))

    def write_ai(self, text: str):
        self.configure(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self.insert("end", f"\n[{ts}] 🤖 Claude AI:\n")
        self.insert("end", "─" * 50 + "\n")
        self.insert("end", text + "\n")
        self.insert("end", "─" * 50 + "\n\n")
        self.configure(state="disabled")
        self.see("end")

    def clear(self):
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")


class ActionButton(ctk.CTkButton):
    def __init__(self, parent, text, command, color=ACCENT, **kwargs):
        super().__init__(parent, text=text, command=command,
                          fg_color=color, hover_color=self._darken(color),
                          font=("Segoe UI", 12, "bold"),
                          corner_radius=6, height=34, **kwargs)

    @staticmethod
    def _darken(hex_color: str) -> str:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"#{max(0,r-30):02x}{max(0,g-30):02x}{max(0,b-30):02x}"


class SectionHeader(ctk.CTkLabel):
    def __init__(self, parent, text, color=ACCENT):
        super().__init__(parent, text=text, font=("Segoe UI", 13, "bold"),
                          text_color=color)


# ─── Panel de conexión ────────────────────────────────────────────────────────

class ConnectionPanel(ctk.CTkFrame):
    def __init__(self, parent, on_connect, on_disconnect):
        super().__init__(parent, fg_color=PANEL_BG, corner_radius=8)
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        ctk.CTkLabel(self, text="IP del dispositivo", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12, pady=(10, 2))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 10))

        self.ip_entry = ctk.CTkEntry(row, placeholder_text="192.168.1.100",
                                      font=("Consolas", 12), width=180)
        self.ip_entry.pack(side="left", padx=(0, 8))
        self.ip_entry.insert(0, "192.168.1.100")

        self.connect_btn = ActionButton(row, "Conectar", self._connect,
                                         color=SUCCESS, width=100)
        self.connect_btn.pack(side="left", padx=(0, 6))

        self.disc_btn = ActionButton(row, "Desconectar", self._disconnect,
                                      color=ACCENT2, width=110, state="disabled")
        self.disc_btn.pack(side="left")

    def _connect(self):
        ip = self.ip_entry.get().strip()
        self.connect_btn.configure(state="disabled", text="Conectando...")
        run_async(ws_connect(ip, 8766))

    def _disconnect(self):
        run_async(ws_disconnect())
        self.set_disconnected()

    def set_connected(self):
        self.connect_btn.configure(state="disabled", text="Conectado")
        self.disc_btn.configure(state="normal")

    def set_disconnected(self):
        self.connect_btn.configure(state="normal", text="Conectar")
        self.disc_btn.configure(state="disabled")


# ─── Tab RF ───────────────────────────────────────────────────────────────────

class RFTab(ctk.CTkFrame):
    def __init__(self, parent, terminal: TerminalBox):
        super().__init__(parent, fg_color="transparent")
        self.terminal = terminal
        self._build()

    def _build(self):
        left = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8, width=220)
        left.pack(side="left", fill="y", padx=(0, 8), pady=0)
        left.pack_propagate(False)

        SectionHeader(left, "⚡  Sub-GHz RF", RF_COLOR).pack(anchor="w", padx=12, pady=(12, 8))

        ctk.CTkLabel(left, text="Frecuencia (MHz)", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.freq = ctk.CTkEntry(left, placeholder_text="433.92", font=("Consolas", 12))
        self.freq.pack(fill="x", padx=12, pady=(2, 8))
        self.freq.insert(0, "433.92")

        ctk.CTkLabel(left, text="Modulación", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.mod = ctk.CTkOptionMenu(left, values=["auto", "OOK", "FSK", "ASK"],
                                      font=("Consolas", 11))
        self.mod.pack(fill="x", padx=12, pady=(2, 8))

        ctk.CTkLabel(left, text="Timeout (seg)", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.timeout = ctk.CTkEntry(left, placeholder_text="20", font=("Consolas", 12))
        self.timeout.pack(fill="x", padx=12, pady=(2, 12))
        self.timeout.insert(0, "20")

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=4)

        btns = [
            ("🔍  Escanear señal",    self._scan,      RF_COLOR),
            ("📡  Ver última captura", self._get_last,  "#444c56"),
            ("🔁  Retransmitir",       self._replay,    "#444c56"),
            ("💥  Bruteforce IA",      self._bruteforce, WARNING),
            ("📊  Espectro RF",        self._spectrum,  "#444c56"),
        ]
        for label, cmd, color in btns:
            ActionButton(left, label, cmd, color=color).pack(fill="x", padx=12, pady=3)

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=8)

        ctk.CTkLabel(left, text="Protocolo bruteforce", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.protocol = ctk.CTkOptionMenu(left,
            values=["Princeton", "CAME", "Nice_Flo", "Keeloq", "Linear", "OOK_auto"],
            font=("Consolas", 11))
        self.protocol.pack(fill="x", padx=12, pady=(2, 12))

        # Right panel - capture preview
        right = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)

        SectionHeader(right, "Captura actual", RF_COLOR).pack(anchor="w", padx=12, pady=(12, 6))

        self.capture_box = TerminalBox(right, height=120)
        self.capture_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _scan(self):
        freq = float(self.freq.get() or 433.92)
        t    = int(self.timeout.get() or 20)
        self.terminal.write(f"Escaneando en {freq} MHz ({t}s)...", prefix="[RF] ")
        send_command({"cmd": "rf_scan", "frequency_mhz": freq,
                       "modulation": self.mod.get(), "timeout_secs": t})

    def _get_last(self):
        self.terminal.write("Obteniendo última captura RF...", prefix="[RF] ")
        send_command({"cmd": "get_file", "dir": "/ext/subghz", "newest": True})

    def _replay(self):
        self.terminal.write("Retransmitiendo última señal...", prefix="[RF] ")
        send_command({"cmd": "rf_transmit", "use_last": True, "repeat": 3})

    def _bruteforce(self):
        proto = self.protocol.get()
        freq  = float(self.freq.get() or 433.92)
        self.terminal.write(f"Solicitando bruteforce IA — {proto} @ {freq} MHz", prefix="[RF] ")
        send_command({"cmd": "generate_bruteforce", "protocol": proto,
                       "bits": 24, "frequency_mhz": freq})

    def _spectrum(self):
        self.terminal.write("Analizando espectro 300-928 MHz...", prefix="[RF] ")
        send_command({"cmd": "rf_spectrum", "start_mhz": 300, "end_mhz": 928, "step_mhz": 1})

    def show_capture(self, content: str):
        self.capture_box.clear()
        self.capture_box.write(content[:800])


# ─── Tab NFC ──────────────────────────────────────────────────────────────────

class NFCTab(ctk.CTkFrame):
    def __init__(self, parent, terminal: TerminalBox):
        super().__init__(parent, fg_color="transparent")
        self.terminal = terminal
        self._build()

    def _build(self):
        left = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8, width=220)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)

        SectionHeader(left, "💳  NFC / RFID", NFC_COLOR).pack(anchor="w", padx=12, pady=(12, 8))

        btns = [
            ("📖  Leer tarjeta",         self._read,    NFC_COLOR),
            ("📂  Último dump",           self._last,    "#444c56"),
            ("🔓  Ataque diccionario",    self._dict,    WARNING),
            ("📋  Emular tarjeta",        self._emulate, "#444c56"),
        ]
        for label, cmd, color in btns:
            ActionButton(left, label, cmd, color=color).pack(fill="x", padx=12, pady=3)

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(left, text="Info", font=("Segoe UI", 10), text_color=TEXT_DIM,
                      wraplength=190).pack(padx=12)
        ctk.CTkLabel(left,
            text="Acerca la tarjeta al\ndispositivo antes de\npulsar Leer.",
            font=("Segoe UI", 10), text_color=TEXT_DIM, justify="left").pack(anchor="w", padx=12)

        right = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)
        SectionHeader(right, "Dump NFC", NFC_COLOR).pack(anchor="w", padx=12, pady=(12, 6))
        self.dump_box = TerminalBox(right)
        self.dump_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _read(self):
        self.terminal.write("Esperando tarjeta NFC (15s)...", prefix="[NFC] ")
        send_command({"cmd": "nfc_read", "timeout_secs": 15})

    def _last(self):
        send_command({"cmd": "get_file", "dir": "/ext/nfc", "newest": True})

    def _dict(self):
        self.terminal.write("Iniciando ataque diccionario Mifare...", prefix="[NFC] ")
        send_command({"cmd": "nfc_dict_attack", "timeout_secs": 60})

    def _emulate(self):
        self.terminal.write("Emulando última tarjeta guardada...", prefix="[NFC] ")
        send_command({"cmd": "nfc_emulate", "use_last": True, "duration_secs": 30})


# ─── Tab WiFi ─────────────────────────────────────────────────────────────────

class WiFiTab(ctk.CTkFrame):
    def __init__(self, parent, terminal: TerminalBox):
        super().__init__(parent, fg_color="transparent")
        self.terminal = terminal
        self._build()

    def _build(self):
        left = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8, width=240)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)

        SectionHeader(left, "📶  WiFi", WIFI_COLOR).pack(anchor="w", padx=12, pady=(12, 8))

        btns = [
            ("🔍  Escanear APs",       self._scan,     WIFI_COLOR),
            ("🤝  Capturar handshake", self._handshake, WARNING),
            ("💥  Deauth ataque",      self._deauth,   ACCENT2),
            ("🕸️  Evil Portal",        self._portal,   "#444c56"),
        ]
        for label, cmd, color in btns:
            ActionButton(left, label, cmd, color=color).pack(fill="x", padx=12, pady=3)

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=8)

        ctk.CTkLabel(left, text="BSSID objetivo", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.bssid = ctk.CTkEntry(left, placeholder_text="AA:BB:CC:DD:EE:FF",
                                   font=("Consolas", 11))
        self.bssid.pack(fill="x", padx=12, pady=(2, 8))

        ctk.CTkLabel(left, text="Canal", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.channel = ctk.CTkEntry(left, placeholder_text="6", font=("Consolas", 11))
        self.channel.pack(fill="x", padx=12, pady=(2, 8))
        self.channel.insert(0, "6")

        ctk.CTkLabel(left, text="SSID (evil portal)", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.ssid = ctk.CTkEntry(left, placeholder_text="MiRed_test", font=("Consolas", 11))
        self.ssid.pack(fill="x", padx=12, pady=(2, 12))

        right = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)
        SectionHeader(right, "Redes detectadas", WIFI_COLOR).pack(anchor="w", padx=12, pady=(12, 6))
        self.ap_box = TerminalBox(right)
        self.ap_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _scan(self):
        self.terminal.write("Escaneando WiFi...", prefix="[WiFi] ")
        send_command({"cmd": "wifi_scan"})

    def _handshake(self):
        bssid = self.bssid.get().strip()
        ch    = int(self.channel.get() or 6)
        if not bssid:
            self.terminal.write("Introduce BSSID objetivo primero.", prefix="[WiFi] ⚠️ ")
            return
        self.terminal.write(f"Capturando handshake de {bssid} canal {ch}...", prefix="[WiFi] ")
        send_command({"cmd": "wifi_handshake", "bssid": bssid, "channel": ch, "timeout_secs": 30})

    def _deauth(self):
        bssid = self.bssid.get().strip()
        ch    = int(self.channel.get() or 6)
        if not bssid:
            self.terminal.write("Introduce BSSID objetivo primero.", prefix="[WiFi] ⚠️ ")
            return
        self.terminal.write(f"Enviando deauth a {bssid}...", prefix="[WiFi] ")
        send_command({"cmd": "wifi_deauth", "bssid": bssid, "channel": ch, "count": 100})

    def _portal(self):
        ssid = self.ssid.get().strip() or "FreeWiFi"
        ch   = int(self.channel.get() or 6)
        self.terminal.write(f"Lanzando evil portal '{ssid}'...", prefix="[WiFi] ")
        send_command({"cmd": "wifi_evil_portal", "ssid": ssid, "channel": ch, "duration_secs": 60})


# ─── Tab BLE ──────────────────────────────────────────────────────────────────

class BLETab(ctk.CTkFrame):
    def __init__(self, parent, terminal: TerminalBox):
        super().__init__(parent, fg_color="transparent")
        self.terminal = terminal
        self._build()

    def _build(self):
        left = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8, width=220)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)

        SectionHeader(left, "🔵  Bluetooth BLE", BLE_COLOR).pack(anchor="w", padx=12, pady=(12, 8))

        btns = [
            ("🔍  Escanear BLE",    self._scan, BLE_COLOR),
            ("📱  Spam Apple",      lambda: self._spam("apple"),   "#555"),
            ("🤖  Spam Android",    lambda: self._spam("android"), "#555"),
            ("🪟  Spam Windows",    lambda: self._spam("windows"), "#555"),
            ("📡  Spam todos",      lambda: self._spam("all"),     WARNING),
        ]
        for label, cmd, color in btns:
            ActionButton(left, label, cmd, color=color).pack(fill="x", padx=12, pady=3)

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(left, text="Duración spam (seg)", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.dur = ctk.CTkEntry(left, font=("Consolas", 11))
        self.dur.pack(fill="x", padx=12, pady=(2, 12))
        self.dur.insert(0, "15")

        right = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)
        SectionHeader(right, "Dispositivos BLE", BLE_COLOR).pack(anchor="w", padx=12, pady=(12, 6))
        self.ble_box = TerminalBox(right)
        self.ble_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _scan(self):
        self.terminal.write("Escaneando BLE (10s)...", prefix="[BLE] ")
        send_command({"cmd": "ble_scan", "duration_secs": 10})

    def _spam(self, target: str):
        dur = int(self.dur.get() or 15)
        self.terminal.write(f"BLE spam → {target} ({dur}s)...", prefix="[BLE] ")
        send_command({"cmd": "ble_spam", "target": target, "duration_secs": dur})


# ─── Tab IR ───────────────────────────────────────────────────────────────────

class IRTab(ctk.CTkFrame):
    def __init__(self, parent, terminal: TerminalBox):
        super().__init__(parent, fg_color="transparent")
        self.terminal = terminal
        self._build()

    def _build(self):
        left = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8, width=220)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)

        SectionHeader(left, "🔴  Infrarrojo", IR_COLOR).pack(anchor="w", padx=12, pady=(12, 8))

        btns = [
            ("📥  Capturar IR",      self._capture,  IR_COLOR),
            ("📤  Retransmitir",     self._replay,   "#444c56"),
        ]
        for label, cmd, color in btns:
            ActionButton(left, label, cmd, color=color).pack(fill="x", padx=12, pady=3)

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=8)
        SectionHeader(left, "Control universal", IR_COLOR).pack(anchor="w", padx=12, pady=(0, 6))

        ctk.CTkLabel(left, text="Tipo", font=("Segoe UI", 11), text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.dev_type = ctk.CTkOptionMenu(left, values=["tv", "ac", "projector", "fan"],
                                           font=("Consolas", 11))
        self.dev_type.pack(fill="x", padx=12, pady=(2, 6))

        ctk.CTkLabel(left, text="Marca", font=("Segoe UI", 11), text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.brand = ctk.CTkEntry(left, placeholder_text="Samsung / LG / Sony...",
                                   font=("Consolas", 11))
        self.brand.pack(fill="x", padx=12, pady=(2, 6))

        ctk.CTkLabel(left, text="Comando", font=("Segoe UI", 11), text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.ir_cmd = ctk.CTkOptionMenu(left,
            values=["power", "volume_up", "volume_down", "mute", "channel_up", "channel_down"],
            font=("Consolas", 11))
        self.ir_cmd.pack(fill="x", padx=12, pady=(2, 8))

        ActionButton(left, "▶  Enviar comando", self._universal, color=IR_COLOR).pack(fill="x", padx=12, pady=3)

        right = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)
        SectionHeader(right, "Señal capturada", IR_COLOR).pack(anchor="w", padx=12, pady=(12, 6))
        self.ir_box = TerminalBox(right)
        self.ir_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _capture(self):
        self.terminal.write("Esperando señal IR (apunta mando al dispositivo)...", prefix="[IR] ")
        send_command({"cmd": "ir_capture", "timeout_secs": 15})

    def _replay(self):
        self.terminal.write("Retransmitiendo última señal IR...", prefix="[IR] ")
        send_command({"cmd": "ir_transmit", "use_last": True, "repeat": 2})

    def _universal(self):
        brand = self.brand.get().strip() or "unknown"
        self.terminal.write(f"IR universal: {self.dev_type.get()} {brand} → {self.ir_cmd.get()}", prefix="[IR] ")
        send_command({"cmd": "ir_universal",
                       "device_type": self.dev_type.get(),
                       "brand": brand,
                       "command": self.ir_cmd.get()})


# ─── Tab AI ───────────────────────────────────────────────────────────────────

class AITab(ctk.CTkFrame):
    def __init__(self, parent, terminal: TerminalBox):
        super().__init__(parent, fg_color="transparent")
        self.terminal = terminal
        self._build()

    def _build(self):
        left = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8, width=240)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)

        SectionHeader(left, "🤖  Claude AI", ACCENT).pack(anchor="w", padx=12, pady=(12, 8))

        btns = [
            ("🔍  Analizar última RF",   lambda: self._analyze("rf"),   RF_COLOR),
            ("💳  Analizar último NFC",  lambda: self._analyze("nfc"),  NFC_COLOR),
            ("📶  Analizar último WiFi", lambda: self._analyze("wifi"), WIFI_COLOR),
            ("🔵  Analizar BLE scan",    lambda: self._analyze("ble"),  BLE_COLOR),
        ]
        for label, cmd, color in btns:
            ActionButton(left, label, cmd, color=color).pack(fill="x", padx=12, pady=3)

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=8)

        SectionHeader(left, "API Key", TEXT_DIM).pack(anchor="w", padx=12, pady=(0, 4))
        self.api_key = ctk.CTkEntry(left, placeholder_text="sk-ant-...",
                                     font=("Consolas", 10), show="*")
        self.api_key.pack(fill="x", padx=12, pady=(0, 6))
        if ANTHROPIC_KEY:
            self.api_key.insert(0, ANTHROPIC_KEY)

        ActionButton(left, "💾  Guardar key", self._save_key, color="#444c56").pack(fill="x", padx=12, pady=3)

        ctk.CTkFrame(left, height=1, fg_color=BORDER).pack(fill="x", padx=12, pady=8)

        ctk.CTkLabel(left, text="Pregunta libre:", font=("Segoe UI", 11),
                      text_color=TEXT_DIM).pack(anchor="w", padx=12)
        self.question = ctk.CTkTextbox(left, font=("Segoe UI", 11), height=80,
                                        fg_color=DARK_BG, corner_radius=6)
        self.question.pack(fill="x", padx=12, pady=(2, 6))
        ActionButton(left, "💬  Preguntar a Claude", self._ask, color=ACCENT).pack(fill="x", padx=12, pady=3)

        right = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)
        SectionHeader(right, "Análisis Claude AI", ACCENT).pack(anchor="w", padx=12, pady=(12, 6))
        self.ai_box = TerminalBox(right)
        self.ai_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.ai_box.write("Claude AI listo. Conecta el dispositivo y analiza capturas.", TEXT_DIM)

    def _save_key(self):
        global ANTHROPIC_KEY
        ANTHROPIC_KEY = self.api_key.get().strip()
        self.terminal.write("API key de Claude guardada.", prefix="[AI] ")

    def _analyze(self, data_type: str):
        self.ai_box.write(f"Analizando {data_type} con Claude...", TEXT_DIM)
        # Get last capture from state and analyze
        last = next((c for c in reversed(state.captures) if c.get("type") == data_type), None)
        if not last:
            self.ai_box.write(f"Sin capturas de tipo '{data_type}' en esta sesión.", ACCENT2)
            return
        analyze_with_claude(last, data_type, lambda r: self.ai_box.write_ai(r))

    def _ask(self):
        q = self.question.get("1.0", "end").strip()
        if not q:
            return
        self.ai_box.write(f"Pregunta: {q}", TEXT_DIM)
        self.question.delete("1.0", "end")

        def _run():
            try:
                key = ANTHROPIC_KEY or self.api_key.get().strip()
                client = anthropic.Anthropic(api_key=key)
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=800,
                    system=SYSTEM_PROMPT + "\n\nContexto de capturas: " + json.dumps(state.captures[-5:]),
                    messages=[{"role": "user", "content": q}]
                )
                self.ai_box.write_ai(response.content[0].text)
            except Exception as e:
                self.ai_box.write(f"Error: {e}", ACCENT2)

        threading.Thread(target=_run, daemon=True).start()


# ─── Ventana principal ────────────────────────────────────────────────────────

class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("LilyGo CC1101 Control Center")
        self.geometry("1280x800")
        self.minsize(900, 600)
        self.configure(fg_color=DARK_BG)

        self._build_ui()
        self._start_bg_thread()
        self._poll_events()

    def _build_ui(self):
        # ── Header
        header = ctk.CTkFrame(self, fg_color=PANEL_BG, corner_radius=0, height=56)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(header, text="  LilyGo CC1101",
                      font=("Segoe UI", 16, "bold"), text_color=ACCENT).pack(side="left", padx=16)
        ctk.CTkLabel(header, text="Control Center  —  Powered by Claude AI",
                      font=("Segoe UI", 11), text_color=TEXT_DIM).pack(side="left")

        self.status_bar = StatusBar(self)

        # ── Main layout
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=12, pady=(8, 4))

        # Left: connection + tabs
        left_col = ctk.CTkFrame(main, fg_color="transparent")
        left_col.pack(side="left", fill="both", expand=True)

        # Connection panel
        self.conn_panel = ConnectionPanel(left_col,
                                           on_connect=self._on_connect,
                                           on_disconnect=self._on_disconnect)
        self.conn_panel.pack(fill="x", pady=(0, 8))

        # Tabs
        self.tabs = ctk.CTkTabview(left_col, fg_color=DARK_BG,
                                    segmented_button_fg_color=PANEL_BG,
                                    segmented_button_selected_color=ACCENT,
                                    segmented_button_selected_hover_color=ACCENT,
                                    text_color=TEXT, text_color_disabled=TEXT_DIM)
        self.tabs.pack(fill="both", expand=True)

        # Add tabs
        for name in ["RF", "NFC", "WiFi", "BLE", "IR", "AI"]:
            self.tabs.add(name)

        # Right: terminal
        right_col = ctk.CTkFrame(main, fg_color="transparent", width=380)
        right_col.pack(side="right", fill="y", padx=(8, 0))
        right_col.pack_propagate(False)

        ctk.CTkLabel(right_col, text="Terminal", font=("Segoe UI", 12, "bold"),
                      text_color=TEXT_DIM).pack(anchor="w", pady=(2, 4))

        self.terminal = TerminalBox(right_col)
        self.terminal.pack(fill="both", expand=True)
        self.terminal.write("LilyGo CC1101 Control Center iniciado.", TEXT_DIM)
        self.terminal.write("Configura la IP y conecta el dispositivo.", TEXT_DIM)

        # Instantiate tab contents
        self.rf_tab   = RFTab(self.tabs.tab("RF"),   self.terminal)
        self.nfc_tab  = NFCTab(self.tabs.tab("NFC"),  self.terminal)
        self.wifi_tab = WiFiTab(self.tabs.tab("WiFi"), self.terminal)
        self.ble_tab  = BLETab(self.tabs.tab("BLE"),  self.terminal)
        self.ir_tab   = IRTab(self.tabs.tab("IR"),   self.terminal)
        self.ai_tab   = AITab(self.tabs.tab("AI"),   self.terminal)

        for tab_widget in [self.rf_tab, self.nfc_tab, self.wifi_tab,
                            self.ble_tab, self.ir_tab, self.ai_tab]:
            tab_widget.pack(fill="both", expand=True)

    def _start_bg_thread(self):
        t = threading.Thread(target=start_event_loop, daemon=True)
        t.start()

    def _on_connect(self):
        pass

    def _on_disconnect(self):
        self.conn_panel.set_disconnected()
        self.status_bar.set_disconnected()

    def _poll_events(self):
        """Process events from the async thread."""
        try:
            while True:
                event_type, data = state.event_queue.get_nowait()
                self._handle_event(event_type, data)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, event_type: str, data: dict):
        if event_type == "connected":
            ip = data["ip"]
            self.conn_panel.set_connected()
            self.status_bar.set_connected(ip)
            self.terminal.write(f"Conectado a LilyGo en {ip}:8766", SUCCESS)
            send_command({"cmd": "ping"})

        elif event_type == "disconnected":
            self.conn_panel.set_disconnected()
            self.status_bar.set_disconnected()
            self.terminal.write("Desconectado del dispositivo.", ACCENT2)

        elif event_type == "error":
            self.terminal.write(data["msg"], ACCENT2)

        elif event_type == "device_data":
            self._process_device_data(data)

    def _process_device_data(self, data: dict):
        dtype = data.get("type") or data.get("status") or "data"
        summary = data.get("summary") or data.get("message") or ""

        # Store capture
        if dtype in ("rf_capture", "nfc_dump", "wifi_scan", "ble_scan", "ir_capture"):
            state.captures.append(data)
            self.terminal.write(f"Captura recibida: {dtype}", SUCCESS)
            count = len(state.captures)
            self.status_bar.set_info(f"Capturas: {count}")
            # Auto-analyze with Claude
            if ANTHROPIC_KEY:
                analyze_with_claude(data, dtype,
                    lambda r: (self.terminal.write_ai(r),
                               self.ai_tab.ai_box.write_ai(r)))

        elif dtype in ("ok", "pong"):
            dev  = data.get("data", {})
            firm = dev.get("firmware", "")
            self.terminal.write(f"Dispositivo listo  {firm}", SUCCESS)

        elif "sequences" in data:
            seqs = data["sequences"]
            self.terminal.write(f"Claude generó {len(seqs)} códigos de bruteforce.", ACCENT)
            for s in seqs[:5]:
                self.terminal.write(f"  {s}", TEXT_DIM)
            if len(seqs) > 5:
                self.terminal.write(f"  ... y {len(seqs)-5} más.", TEXT_DIM)

        elif "content" in data.get("data", {}):
            content = data["data"]["content"]
            path    = data["data"].get("path", "")
            self.terminal.write(f"Archivo: {path}", TEXT_DIM)
            if path.endswith(".sub"):
                self.rf_tab.show_capture(content)
            elif path.endswith(".nfc"):
                self.nfc_tab.dump_box.clear()
                self.nfc_tab.dump_box.write(content[:600])

        elif "error" in data:
            self.terminal.write(f"Error dispositivo: {data['error']}", ACCENT2)

        else:
            if summary:
                self.terminal.write(summary)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
