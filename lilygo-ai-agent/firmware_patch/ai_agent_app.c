/**
 * AI Agent v2 - LilyGo CC1101 / Flipper Zero ESP32 Port
 *
 * Mantiene TODAS las funcionalidades del firmware Flipper Zero y añade:
 *  - Servidor WebSocket en puerto 8766 (recibe comandos del MCP de Claude)
 *  - Cliente WebSocket en puerto 8765 (envía capturas al servidor Python)
 *  - Auto-análisis: cada captura RF/NFC/WiFi se manda automáticamente a Claude
 *  - Resultados de IA mostrados en pantalla con overlay sobre el UI normal
 *  - Menú AI Agent integrado en el menú principal
 *
 * INSTALAR:
 *   cp ai_agent_app.c  <repo>/applications_user/ai_agent/
 *   cp mcp_ws_server.c <repo>/applications_user/ai_agent/
 *   Añadir a fam_config.py (ver ai_agent_fam.py)
 *   ./build.sh lilygo-t-embed-cc1101
 */

#include <furi.h>
#include <furi_hal.h>
#include <gui/gui.h>
#include <gui/view_dispatcher.h>
#include <gui/modules/submenu.h>
#include <gui/modules/text_input.h>
#include <gui/modules/popup.h>
#include <gui/modules/loading.h>
#include <gui/modules/widget.h>
#include <storage/storage.h>
#include <notification/notification_messages.h>
#include <esp_websocket_client.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <cJSON.h>
#include <string.h>
#include <stdio.h>

/* ─── Constantes ─────────────────────────────────────────────────────────── */

#define AI_TAG          "AIAgent"
#define WS_URI_MAX      128
#define RESULT_MAX      600
#define SETTINGS_PATH   "/ext/ai_agent/settings.json"
#define AI_LOG_PATH     "/ext/ai_agent/ai_log.jsonl"
#define AUTO_ANALYZE    true   /* Analizar automáticamente cada captura */

/* ─── Estados ────────────────────────────────────────────────────────────── */

typedef enum {
    ViewMenu = 0,
    ViewResult,
    ViewLoading,
    ViewSettings,
    ViewLog,
} ViewID;

typedef enum {
    MenuAnalyzeRF = 0,
    MenuAnalyzeNFC,
    MenuAnalyzeWiFi,
    MenuBruteforce,
    MenuSpectrum,
    MenuAutoMode,
    MenuViewLog,
    MenuSettings,
    MenuDisconnect,
} MenuIdx;

typedef struct {
    /* UI */
    Gui*            gui;
    ViewDispatcher* vd;
    Submenu*        menu;
    Popup*          popup;
    Loading*        loading;
    TextInput*      text_input;
    Widget*         log_widget;

    /* WebSocket (cliente → servidor Python) */
    esp_websocket_client_handle_t ws_out;
    bool  ws_out_connected;

    /* Configuración */
    char  server_ip[64];
    char  server_uri[WS_URI_MAX];
    char  mcp_uri[WS_URI_MAX];  /* URI para el servidor MCP (puerto 8766) */

    /* Estado */
    char  result[RESULT_MAX];
    bool  auto_mode;            /* Si true, analiza cada captura automáticamente */
    bool  waiting_ai;
    uint32_t captures_count;
    uint32_t ai_requests_count;
} AIApp;

/* ─── Persistencia ───────────────────────────────────────────────────────── */

static void settings_load(AIApp* app) {
    snprintf(app->server_ip, sizeof(app->server_ip), "192.168.1.100");
    app->auto_mode = AUTO_ANALYZE;

    Storage* st = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(st);
    if(storage_file_open(f, SETTINGS_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        char buf[384] = {0};
        storage_file_read(f, buf, sizeof(buf) - 1);
        storage_file_close(f);
        cJSON* r = cJSON_Parse(buf);
        if(r) {
            cJSON* ip   = cJSON_GetObjectItem(r, "server_ip");
            cJSON* amod = cJSON_GetObjectItem(r, "auto_mode");
            if(ip   && cJSON_IsString(ip))  snprintf(app->server_ip, sizeof(app->server_ip), "%s", ip->valuestring);
            if(amod && cJSON_IsBool(amod))  app->auto_mode = cJSON_IsTrue(amod);
            cJSON_Delete(r);
        }
    }
    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    snprintf(app->server_uri, sizeof(app->server_uri), "ws://%s:8765", app->server_ip);
    snprintf(app->mcp_uri,    sizeof(app->mcp_uri),    "ws://%s:8766", app->server_ip);
}

static void settings_save(AIApp* app) {
    Storage* st = furi_record_open(RECORD_STORAGE);
    storage_common_mkdir(st, "/ext/ai_agent");
    File* f = storage_file_alloc(st);
    if(storage_file_open(f, SETTINGS_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        cJSON* r = cJSON_CreateObject();
        cJSON_AddStringToObject(r, "server_ip", app->server_ip);
        cJSON_AddBoolToObject(r,  "auto_mode",  app->auto_mode);
        char* out = cJSON_PrintUnformatted(r);
        storage_file_write(f, out, strlen(out));
        free(out);
        cJSON_Delete(r);
        storage_file_close(f);
    }
    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);
}

static void log_event(const char* event_type, const char* summary) {
    Storage* st = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(st);
    if(storage_file_open(f, AI_LOG_PATH, FSAM_WRITE, FSOM_OPEN_APPEND)) {
        cJSON* entry = cJSON_CreateObject();
        cJSON_AddStringToObject(entry, "type", event_type);
        cJSON_AddStringToObject(entry, "summary", summary);
        char* out = cJSON_PrintUnformatted(entry);
        storage_file_write(f, out, strlen(out));
        storage_file_write(f, "\n", 1);
        free(out);
        cJSON_Delete(entry);
        storage_file_close(f);
    }
    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);
}

/* ─── WebSocket (salida → servidor Python) ───────────────────────────────── */

static void ws_event_handler(void* arg, esp_event_base_t base,
                              int32_t event_id, void* event_data) {
    AIApp* app = (AIApp*)arg;
    esp_websocket_event_data_t* d = (esp_websocket_event_data_t*)event_data;

    switch(event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        app->ws_out_connected = true;
        FURI_LOG_I(AI_TAG, "WS connected to AI server");
        break;

    case WEBSOCKET_EVENT_DISCONNECTED:
        app->ws_out_connected = false;
        break;

    case WEBSOCKET_EVENT_DATA:
        if(d->data_ptr && d->data_len > 0) {
            char* buf = malloc(d->data_len + 1);
            if(buf) {
                memcpy(buf, d->data_ptr, d->data_len);
                buf[d->data_len] = '\0';

                cJSON* root = cJSON_Parse(buf);
                if(root) {
                    /* Prioridad: summary > analysis > message */
                    const char* fields[] = {"summary", "analysis", "message", NULL};
                    for(int i = 0; fields[i]; i++) {
                        cJSON* j = cJSON_GetObjectItem(root, fields[i]);
                        if(j && cJSON_IsString(j)) {
                            snprintf(app->result, RESULT_MAX, "%s", j->valuestring);
                            log_event("ai_response", app->result);
                            break;
                        }
                    }
                    /* Bruteforce: mostrar códigos generados */
                    cJSON* seqs = cJSON_GetObjectItem(root, "sequences");
                    if(seqs && cJSON_IsArray(seqs)) {
                        int n = cJSON_GetArraySize(seqs);
                        snprintf(app->result, RESULT_MAX, "Claude generó %d códigos.\nEjecutando...", n);
                        /* TODO: pasar al módulo SubGHz para transmisión */
                    }
                    cJSON_Delete(root);
                }
                free(buf);
                app->waiting_ai = false;
                app->ai_requests_count++;

                /* Vibración corta cuando llega respuesta de IA */
                NotificationApp* notif = furi_record_open(RECORD_NOTIFICATION);
                notification_message(notif, &sequence_single_vibro);
                furi_record_close(RECORD_NOTIFICATION);
            }
        }
        break;

    default: break;
    }
}

static bool ws_connect(AIApp* app) {
    if(app->ws_out_connected) return true;
    esp_websocket_client_config_t cfg = {
        .uri                  = app->server_uri,
        .reconnect_timeout_ms = 4000,
        .network_timeout_ms   = 6000,
    };
    app->ws_out = esp_websocket_client_init(&cfg);
    esp_websocket_register_events(app->ws_out, WEBSOCKET_EVENT_ANY, ws_event_handler, app);
    return esp_websocket_client_start(app->ws_out) == ESP_OK;
}

static void ws_disconnect(AIApp* app) {
    if(app->ws_out) {
        esp_websocket_client_stop(app->ws_out);
        esp_websocket_client_destroy(app->ws_out);
        app->ws_out           = NULL;
        app->ws_out_connected = false;
    }
}

static bool ws_send(AIApp* app, const char* json) {
    if(!app->ws_out_connected) return false;
    app->waiting_ai = true;
    app->captures_count++;
    return esp_websocket_client_send_text(app->ws_out, json, strlen(json), pdMS_TO_TICKS(4000)) >= 0;
}

/* ─── Lectura de archivos ────────────────────────────────────────────────── */

typedef struct { char path[256]; char content[2048]; } FileData;

static bool read_newest(const char* dir, FileData* out) {
    Storage* st = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(st);
    FileInfo info;
    uint64_t newest_ts = 0;
    out->path[0] = '\0';

    if(storage_dir_open(f, dir)) {
        char fname[128];
        while(storage_dir_read(f, &info, fname, sizeof(fname))) {
            if(!(info.flags & FSF_DIRECTORY) && info.modified_date > newest_ts) {
                newest_ts = info.modified_date;
                snprintf(out->path, sizeof(out->path), "%s/%s", dir, fname);
            }
        }
        storage_dir_close(f);
    }

    bool ok = false;
    if(strlen(out->path) && storage_file_open(f, out->path, FSAM_READ, FSOM_OPEN_EXISTING)) {
        storage_file_read(f, out->content, sizeof(out->content) - 1);
        storage_file_close(f);
        ok = true;
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);
    return ok;
}

/* ─── Envíos al servidor AI ──────────────────────────────────────────────── */

static void send_rf(AIApp* app) {
    FileData fd = {0};
    if(!read_newest("/ext/subghz", &fd)) {
        snprintf(app->result, RESULT_MAX, "Sin capturas RF.\nUsa SubGHz primero.");
        return;
    }
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "rf_capture");
    cJSON* d = cJSON_CreateObject();
    cJSON_AddStringToObject(d, "file",    fd.path);
    cJSON_AddStringToObject(d, "content", fd.content);
    cJSON_AddItemToObject(root, "data", d);
    char* msg = cJSON_PrintUnformatted(root);
    if(!ws_send(app, msg))
        snprintf(app->result, RESULT_MAX, "Error enviando.\nConecta primero.");
    else
        snprintf(app->result, RESULT_MAX, "Enviando a Claude AI...\nEspera respuesta.");
    free(msg);
    cJSON_Delete(root);
    log_event("rf_sent", fd.path);
}

static void send_nfc(AIApp* app) {
    FileData fd = {0};
    if(!read_newest("/ext/nfc", &fd)) {
        snprintf(app->result, RESULT_MAX, "Sin dumps NFC.\nUsa NFC Reader primero.");
        return;
    }
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "nfc_dump");
    cJSON* d = cJSON_CreateObject();
    cJSON_AddStringToObject(d, "file",    fd.path);
    cJSON_AddStringToObject(d, "raw",     fd.content);
    cJSON_AddItemToObject(root, "data", d);
    char* msg = cJSON_PrintUnformatted(root);
    ws_send(app, msg);
    snprintf(app->result, RESULT_MAX, "NFC enviado a Claude AI...");
    free(msg);
    cJSON_Delete(root);
    log_event("nfc_sent", fd.path);
}

static void send_wifi(AIApp* app) {
    FileData fd = {0};
    /* WiFi scan se guarda como last_scan.json */
    Storage* st = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(st);
    snprintf(fd.path, sizeof(fd.path), "/ext/wifi/last_scan.json");
    bool ok = false;
    if(storage_file_open(f, fd.path, FSAM_READ, FSOM_OPEN_EXISTING)) {
        storage_file_read(f, fd.content, sizeof(fd.content) - 1);
        storage_file_close(f);
        ok = true;
    }
    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    if(!ok) {
        snprintf(app->result, RESULT_MAX, "Sin escaneo WiFi.\nUsa WiFi Scanner primero.");
        return;
    }
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "wifi_scan");
    cJSON* data = cJSON_Parse(fd.content);
    if(data) cJSON_AddItemToObject(root, "data", data);
    else     cJSON_AddStringToObject(root, "data", fd.content);
    char* msg = cJSON_PrintUnformatted(root);
    ws_send(app, msg);
    snprintf(app->result, RESULT_MAX, "WiFi scan enviado a Claude...");
    free(msg);
    cJSON_Delete(root);
    log_event("wifi_sent", fd.path);
}

static void request_bruteforce(AIApp* app) {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "command");
    cJSON_AddStringToObject(root, "cmd",  "generate_bruteforce");
    /* Detectar protocolo del último .sub */
    FileData fd = {0};
    if(read_newest("/ext/subghz", &fd)) {
        /* Parsear protocolo del archivo .sub */
        char* proto = strstr(fd.content, "Preset:");
        if(proto) {
            char proto_name[32] = {0};
            sscanf(proto + 8, "%31s", proto_name);
            cJSON_AddStringToObject(root, "protocol", proto_name);
        } else {
            cJSON_AddStringToObject(root, "protocol", "OOK");
        }
    } else {
        cJSON_AddStringToObject(root, "protocol", "OOK");
    }
    cJSON_AddNumberToObject(root, "bits", 24);
    cJSON_AddNumberToObject(root, "frequency_mhz", 433.92);
    char* msg = cJSON_PrintUnformatted(root);
    ws_send(app, msg);
    snprintf(app->result, RESULT_MAX, "Claude genera códigos\ninteligentes...");
    free(msg);
    cJSON_Delete(root);
}

static void request_spectrum(AIApp* app) {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "command");
    cJSON_AddStringToObject(root, "cmd",  "rf_spectrum");
    cJSON_AddNumberToObject(root, "start_mhz", 300.0);
    cJSON_AddNumberToObject(root, "end_mhz",   928.0);
    char* msg = cJSON_PrintUnformatted(root);
    ws_send(app, msg);
    snprintf(app->result, RESULT_MAX, "Analizando espectro RF...");
    free(msg);
    cJSON_Delete(root);
}

/* ─── Auto-mode hook ─────────────────────────────────────────────────────── */

/* Llamar desde el hook de captura completada en SubGHz/NFC/WiFi apps */
void ai_agent_auto_analyze(AIApp* app, const char* type) {
    if(!app || !app->auto_mode || !app->ws_out_connected) return;
    if(strcmp(type, "rf")   == 0) send_rf(app);
    if(strcmp(type, "nfc")  == 0) send_nfc(app);
    if(strcmp(type, "wifi") == 0) send_wifi(app);
}

/* ─── UI ─────────────────────────────────────────────────────────────────── */

static void show_result(AIApp* app) {
    popup_set_text(app->popup, app->result, 64, 38, AlignCenter, AlignCenter);
    view_dispatcher_switch_to_view(app->vd, ViewResult);
}

static void menu_cb(void* ctx, uint32_t idx) {
    AIApp* app = (AIApp*)ctx;
    bool need_conn = (idx != MenuSettings && idx != MenuDisconnect && idx != MenuAutoMode && idx != MenuViewLog);

    if(need_conn && !app->ws_out_connected) {
        snprintf(app->result, RESULT_MAX,
            "No conectado.\nIr a Settings primero.\n\nIP: %s", app->server_ip);
        show_result(app);
        return;
    }

    switch((MenuIdx)idx) {
    case MenuAnalyzeRF:
        snprintf(app->result, RESULT_MAX, "Enviando RF a Claude AI...");
        show_result(app);
        send_rf(app);
        break;
    case MenuAnalyzeNFC:
        snprintf(app->result, RESULT_MAX, "Enviando NFC a Claude AI...");
        show_result(app);
        send_nfc(app);
        break;
    case MenuAnalyzeWiFi:
        snprintf(app->result, RESULT_MAX, "Enviando WiFi a Claude AI...");
        show_result(app);
        send_wifi(app);
        break;
    case MenuBruteforce:
        snprintf(app->result, RESULT_MAX, "Generando códigos\ncon Claude AI...");
        show_result(app);
        request_bruteforce(app);
        break;
    case MenuSpectrum:
        snprintf(app->result, RESULT_MAX, "Analizando espectro RF...");
        show_result(app);
        request_spectrum(app);
        break;
    case MenuAutoMode:
        app->auto_mode = !app->auto_mode;
        settings_save(app);
        snprintf(app->result, RESULT_MAX,
            "Modo automático: %s\n\nCada captura se enviará\nautomáticamente a Claude.",
            app->auto_mode ? "ACTIVADO" : "DESACTIVADO");
        show_result(app);
        break;
    case MenuViewLog:
        snprintf(app->result, RESULT_MAX,
            "Capturas: %lu\nConsultas IA: %lu\nConectado: %s",
            (unsigned long)app->captures_count,
            (unsigned long)app->ai_requests_count,
            app->ws_out_connected ? "SI" : "NO");
        show_result(app);
        break;
    case MenuSettings:
        view_dispatcher_switch_to_view(app->vd, ViewSettings);
        return;
    case MenuDisconnect:
        ws_disconnect(app);
        snprintf(app->result, RESULT_MAX, "Desconectado del\nservidor AI.");
        show_result(app);
        break;
    }
}

static uint32_t back_to_menu(void* ctx) { UNUSED(ctx); return ViewMenu; }

static void settings_done_cb(void* ctx) {
    AIApp* app = (AIApp*)ctx;
    settings_save(app);
    snprintf(app->server_uri, sizeof(app->server_uri), "ws://%s:8765", app->server_ip);
    snprintf(app->mcp_uri,    sizeof(app->mcp_uri),    "ws://%s:8766", app->server_ip);

    snprintf(app->result, RESULT_MAX, "Conectando a\n%s...", app->server_ip);
    show_result(app);

    if(ws_connect(app)) {
        furi_delay_ms(1500);
        if(app->ws_out_connected)
            snprintf(app->result, RESULT_MAX, "Conectado!\nClaude AI listo.\n\nAuto-mode: %s",
                app->auto_mode ? "ON" : "OFF");
        else
            snprintf(app->result, RESULT_MAX, "Conexión fallida.\nVerifica IP y servidor.");
    } else {
        snprintf(app->result, RESULT_MAX, "Error al iniciar\nconexión WebSocket.");
    }
    popup_set_text(app->popup, app->result, 64, 38, AlignCenter, AlignCenter);
}

/* ─── App lifecycle ──────────────────────────────────────────────────────── */

static AIApp* app_alloc(void) {
    AIApp* app = malloc(sizeof(AIApp));
    memset(app, 0, sizeof(AIApp));

    app->gui = furi_record_open(RECORD_GUI);
    app->vd  = view_dispatcher_alloc();
    view_dispatcher_enable_queue(app->vd);
    view_dispatcher_attach_to_gui(app->vd, app->gui, ViewDispatcherTypeFullscreen);

    /* Menú */
    app->menu = submenu_alloc();
    submenu_set_header(app->menu, "AI Agent v2");
    submenu_add_item(app->menu, "Analizar RF",      MenuAnalyzeRF,   menu_cb, app);
    submenu_add_item(app->menu, "Analizar NFC",     MenuAnalyzeNFC,  menu_cb, app);
    submenu_add_item(app->menu, "Analizar WiFi",    MenuAnalyzeWiFi, menu_cb, app);
    submenu_add_item(app->menu, "Bruteforce IA",    MenuBruteforce,  menu_cb, app);
    submenu_add_item(app->menu, "Espectro RF",      MenuSpectrum,    menu_cb, app);
    submenu_add_item(app->menu, "Auto-mode ON/OFF", MenuAutoMode,    menu_cb, app);
    submenu_add_item(app->menu, "Ver estadísticas", MenuViewLog,     menu_cb, app);
    submenu_add_item(app->menu, "Ajustes / Conectar", MenuSettings,  menu_cb, app);
    submenu_add_item(app->menu, "Desconectar",      MenuDisconnect,  menu_cb, app);
    view_dispatcher_add_view(app->vd, ViewMenu, submenu_get_view(app->menu));

    /* Popup (resultados) */
    app->popup = popup_alloc();
    popup_set_header(app->popup, "Claude AI", 64, 6, AlignCenter, AlignTop);
    popup_set_callback(app->popup, back_to_menu);
    popup_set_context(app->popup, app);
    view_dispatcher_add_view(app->vd, ViewResult, popup_get_view(app->popup));

    /* Loading */
    app->loading = loading_alloc();
    view_dispatcher_add_view(app->vd, ViewLoading, loading_get_view(app->loading));

    /* Text input (IP del servidor) */
    app->text_input = text_input_alloc();
    text_input_set_header_text(app->text_input, "IP del servidor AI:");
    text_input_set_result_callback(app->text_input, settings_done_cb, app,
                                    app->server_ip, sizeof(app->server_ip), true);
    view_dispatcher_add_view(app->vd, ViewSettings, text_input_get_view(app->text_input));

    settings_load(app);
    return app;
}

static void app_free(AIApp* app) {
    ws_disconnect(app);
    view_dispatcher_remove_view(app->vd, ViewMenu);
    view_dispatcher_remove_view(app->vd, ViewResult);
    view_dispatcher_remove_view(app->vd, ViewLoading);
    view_dispatcher_remove_view(app->vd, ViewSettings);
    submenu_free(app->menu);
    popup_free(app->popup);
    loading_free(app->loading);
    text_input_free(app->text_input);
    view_dispatcher_free(app->vd);
    furi_record_close(RECORD_GUI);
    free(app);
}

int32_t ai_agent_app(void* p) {
    UNUSED(p);
    AIApp* app = app_alloc();

    /* Intentar autoconectar si ya hay IP configurada */
    if(strlen(app->server_ip) > 7) {
        ws_connect(app);
        furi_delay_ms(800);
    }

    view_dispatcher_switch_to_view(app->vd, ViewMenu);
    view_dispatcher_run(app->vd);
    app_free(app);
    return 0;
}
