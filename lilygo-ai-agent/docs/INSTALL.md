# LilyGo CC1101 AI Agent - Guía de instalación

## Arquitectura

```
LilyGo T-Embed CC1101
  └─ WiFi ──> WebSocket ──> PC Server (Python)
                                └─> Claude AI API
                                └─> Análisis en tiempo real
                                └─> Capturas guardadas en disco
```

## Paso 1: Servidor PC

```bash
cd pc_server
cp .env.example .env
# Edita .env y pon tu ANTHROPIC_API_KEY

pip install -r requirements.txt
python server.py
```

El servidor escucha en el puerto **8765**. Anota la IP de tu PC en la red local
(ej. `192.168.1.100`).

## Paso 2: Firmware

### Opción A: Compilar desde fuente

1. Clona el firmware base:
   ```bash
   git clone https://github.com/Sor3nt/Flipper-Zero-ESP32-Port
   cd Flipper-Zero-ESP32-Port
   ```

2. Copia el módulo:
   ```bash
   mkdir -p applications_user/ai_agent
   cp /path/to/firmware_patch/ai_agent_app.c applications_user/ai_agent/
   ```

3. Añade al manifest. En `fam_config.py` agrega al final:
   ```python
   App(
       appid="ai_agent",
       name="AI Agent",
       apptype=FlipperAppType.EXTERNAL,
       entry_point="ai_agent_app",
       requires=["gui", "storage"],
       stack_size=4 * 1024,
       order=90,
       fap_category="Tools",
       sources=["ai_agent_app.c"],
   )
   ```

4. Compila y flashea:
   ```bash
   ./build.sh lilygo-t-embed-cc1101
   ```

### Opción B: Solo el servidor (sin recompilar)

Si no quieres recompilar, el servidor también funciona como receptor pasivo:
puedes exportar capturas `.sub` / `.nfc` manualmente a la carpeta `captures/`
y analizarlas con:

```bash
python analyze_file.py captures/mi_captura.sub
```

## Paso 3: Usar en el dispositivo

1. Asegúrate de que el LilyGo está conectado a tu WiFi
2. Ve a: **Apps > Tools > AI Agent**
3. Selecciona **Settings / Connect**
4. Introduce la IP de tu PC
5. Cuando conecte, usa el menú:
   - **Analyze Last RF** → analiza el último archivo `.sub`
   - **Analyze Last WiFi** → analiza el último escaneo WiFi
   - **Analyze Last NFC** → analiza el último dump NFC
   - **Smart Bruteforce** → Claude genera códigos inteligentes para probar

## Qué hace Claude con las capturas

### RF / Sub-GHz
- Identifica el protocolo (OOK, FSK, Princeton, HCS, etc.)
- Detecta el tipo de dispositivo (mando garaje, alarma, sensor, etc.)
- Sugiere si es vulnerable a replay
- Genera secuencias de brute-force inteligentes

### WiFi
- Analiza los APs encontrados
- Identifica configuraciones débiles (WPS activo, WEP, SSID ocultos)
- Sugiere qué handshakes capturar primero
- Prioriza targets

### NFC
- Identifica el tipo de tarjeta (Mifare Classic, NTAG, EMV, etc.)
- Indica si es clonable
- Sugiere ataque de diccionario específico para el sector/key

## Seguridad

Este sistema está diseñado para **seguridad ofensiva autorizada**:
- Auditorías de tus propios sistemas
- CTF competitions
- Investigación de RF en entornos controlados

No uses estas herramientas en sistemas que no tienes permiso de auditar.

## Estructura de archivos

```
lilygo-ai-agent/
├── firmware_patch/
│   ├── ai_agent_app.c      # Módulo ESP32 (C)
│   └── ai_agent_fam.py     # Manifest de aplicación
├── pc_server/
│   ├── server.py           # Servidor WebSocket + Claude
│   ├── requirements.txt
│   └── .env.example
└── docs/
    └── INSTALL.md          # Esta guía
```

## Ejemplo de análisis

El dispositivo captura una señal Sub-GHz → la envía al servidor → Claude responde:

```
PROTOCOL: Princeton (OOK, 433MHz)
DEVICE: Mando garaje residencial ~2019
ACTION: Vulnerable a replay. Guarda con: subghz save garage_01
RISK: MEDIUM - replay directo funciona si no usa rolling codes
```
