/**
 * AI Agent Application - LilyGo CC1101 / Flipper Zero ESP32 Port
 *
 * Connects the device to a PC AI server via WebSocket.
 * Sends captures (RF, WiFi, NFC) for Claude AI analysis.
 *
 * INSTALL:
 *   Copy this file to: applications_user/ai_agent/
 *   Add entry to fam_config.py (see ai_agent_fam.py)
 *   Rebuild firmware
 */

#include <furi.h>
#include <furi_hal.h>
#include <gui/gui.h>
#include <gui/view_dispatcher.h>
#include <gui/modules/submenu.h>
#include <gui/modules/text_input.h>
#include <gui/modules/popup.h>
#include <gui/modules/loading.h>
#include <storage/storage.h>
#include <esp_websocket_client.h>
#include <esp_wifi.h>
#include <cJSON.h>
#include <string.h>
#include <stdio.h>

#define AI_AGENT_TAG "AIAgent"
#define WS_URI_MAX   128
#define MSG_MAX      2048
#define RESULT_MAX   512
#define SETTINGS_PATH "/ext/ai_agent/settings.json"
#define CAPTURES_PATH "/ext/ai_agent/captures/"

/* ─── State ─────────────────────────────────────────────── */

typedef enum {
    AIAgentStateMenu,
    AIAgentStateConnect,
    AIAgentStateConnected,
    AIAgentStateAnalyzing,
    AIAgentStateResult,
    AIAgentStateSettings,
} AIAgentState;

typedef enum {
    MenuAnalyzeLastRF = 0,
    MenuAnalyzeLastWiFi,
    MenuAnalyzeLastNFC,
    MenuBruteforce,
    MenuSavedCaptures,
    MenuSettings,
    MenuDisconnect,
} MenuIndex;

typedef struct {
    /* UI */
    Gui*            gui;
    ViewDispatcher* view_dispatcher;
    Submenu*        menu;
    Popup*          popup;
    Loading*        loading;
    TextInput*      text_input;

    /* WebSocket */
    esp_websocket_client_handle_t ws_client;
    bool  connected;
    bool  waiting_response;

    /* Data */
    char  server_uri[WS_URI_MAX];
    char  server_ip[64];
    char  result_text[RESULT_MAX];
    AIAgentState state;

    /* Task handle */
    FuriThread* ws_thread;
} AIAgentApp;

/* ─── Settings ───────────────────────────────────────────── */

static void ai_agent_load_settings(AIAgentApp* app) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);

    snprintf(app->server_ip, sizeof(app->server_ip), "192.168.1.100");

    if(storage_file_open(f, SETTINGS_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        char buf[256] = {0};
        storage_file_read(f, buf, sizeof(buf) - 1);
        storage_file_close(f);

        cJSON* root = cJSON_Parse(buf);
        if(root) {
            cJSON* ip = cJSON_GetObjectItem(root, "server_ip");
            if(ip && cJSON_IsString(ip))
                snprintf(app->server_ip, sizeof(app->server_ip), "%s", ip->valuestring);
            cJSON_Delete(root);
        }
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    snprintf(app->server_uri, sizeof(app->server_uri), "ws://%s:8765", app->server_ip);
}

static void ai_agent_save_settings(AIAgentApp* app) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_common_mkdir(storage, "/ext/ai_agent");

    File* f = storage_file_alloc(storage);
    if(storage_file_open(f, SETTINGS_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        cJSON* root = cJSON_CreateObject();
        cJSON_AddStringToObject(root, "server_ip", app->server_ip);
        char* out = cJSON_PrintUnformatted(root);
        storage_file_write(f, out, strlen(out));
        free(out);
        cJSON_Delete(root);
        storage_file_close(f);
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);
}

/* ─── WebSocket ──────────────────────────────────────────── */

static void ws_event_handler(void* handler_args,
                              esp_event_base_t base,
                              int32_t event_id,
                              void* event_data)
{
    AIAgentApp* app = (AIAgentApp*)handler_args;
    esp_websocket_event_data_t* data = (esp_websocket_event_data_t*)event_data;

    switch(event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        FURI_LOG_I(AI_AGENT_TAG, "WebSocket connected");
        app->connected = true;
        break;

    case WEBSOCKET_EVENT_DISCONNECTED:
        FURI_LOG_I(AI_AGENT_TAG, "WebSocket disconnected");
        app->connected = false;
        break;

    case WEBSOCKET_EVENT_DATA:
        if(data->data_ptr && data->data_len > 0) {
            char* buf = malloc(data->data_len + 1);
            if(buf) {
                memcpy(buf, data->data_ptr, data->data_len);
                buf[data->data_len] = '\0';

                cJSON* root = cJSON_Parse(buf);
                if(root) {
                    cJSON* summary = cJSON_GetObjectItem(root, "summary");
                    cJSON* analysis = cJSON_GetObjectItem(root, "analysis");
                    cJSON* message  = cJSON_GetObjectItem(root, "message");

                    const char* text = NULL;
                    if(summary && cJSON_IsString(summary))       text = summary->valuestring;
                    else if(message && cJSON_IsString(message))  text = message->valuestring;
                    else if(analysis && cJSON_IsString(analysis)) text = analysis->valuestring;

                    if(text) snprintf(app->result_text, RESULT_MAX, "%s", text);
                    cJSON_Delete(root);
                }
                free(buf);
                app->waiting_response = false;
            }
        }
        break;

    default:
        break;
    }
}

static bool ai_agent_ws_connect(AIAgentApp* app) {
    esp_websocket_client_config_t cfg = {
        .uri = app->server_uri,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms   = 5000,
    };

    app->ws_client = esp_websocket_client_init(&cfg);
    esp_websocket_register_events(app->ws_client, WEBSOCKET_EVENT_ANY,
                                   ws_event_handler, app);
    return esp_websocket_client_start(app->ws_client) == ESP_OK;
}

static void ai_agent_ws_disconnect(AIAgentApp* app) {
    if(app->ws_client) {
        esp_websocket_client_stop(app->ws_client);
        esp_websocket_client_destroy(app->ws_client);
        app->ws_client  = NULL;
        app->connected  = false;
    }
}

static bool ai_agent_send(AIAgentApp* app, const char* json_str) {
    if(!app->connected || !app->ws_client) return false;
    app->waiting_response = true;
    int ret = esp_websocket_client_send_text(app->ws_client, json_str, strlen(json_str), pdMS_TO_TICKS(3000));
    return ret >= 0;
}

/* ─── Capture helpers ────────────────────────────────────── */

/* Read last SubGHz capture from SD card and send to AI */
static void ai_analyze_last_rf(AIAgentApp* app) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);

    /* Find most recent .sub file */
    const char* sub_dir = "/ext/subghz";
    FileInfo info;
    char filepath[256];
    char newest[256] = {0};
    uint64_t newest_ts = 0;

    if(storage_dir_open(f, sub_dir)) {
        char fname[128];
        while(storage_dir_read(f, &info, fname, sizeof(fname))) {
            if(!(info.flags & FSF_DIRECTORY)) {
                snprintf(filepath, sizeof(filepath), "%s/%s", sub_dir, fname);
                if(info.modified_date > newest_ts) {
                    newest_ts = info.modified_date;
                    snprintf(newest, sizeof(newest), "%s", filepath);
                }
            }
        }
        storage_dir_close(f);
    }

    if(strlen(newest) == 0) {
        snprintf(app->result_text, RESULT_MAX, "No SubGHz captures found.\nCapture a signal first.");
        storage_file_free(f);
        furi_record_close(RECORD_STORAGE);
        return;
    }

    char content[1024] = {0};
    if(storage_file_open(f, newest, FSAM_READ, FSOM_OPEN_EXISTING)) {
        storage_file_read(f, content, sizeof(content) - 1);
        storage_file_close(f);
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "rf_capture");
    cJSON* data = cJSON_CreateObject();
    cJSON_AddStringToObject(data, "file", newest);
    cJSON_AddStringToObject(data, "content", content);
    cJSON_AddItemToObject(root, "data", data);

    char* msg = cJSON_PrintUnformatted(root);
    ai_agent_send(app, msg);
    free(msg);
    cJSON_Delete(root);
}

static void ai_analyze_last_wifi(AIAgentApp* app) {
    /* Read last WiFi scan result - stored by the WiFi app as JSON */
    const char* scan_file = "/ext/wifi/last_scan.json";
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);
    char content[2048] = {0};

    if(storage_file_open(f, scan_file, FSAM_READ, FSOM_OPEN_EXISTING)) {
        storage_file_read(f, content, sizeof(content) - 1);
        storage_file_close(f);
    } else {
        snprintf(content, sizeof(content), "{\"note\":\"No saved scan. Run WiFi Scanner first.\"}");
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "wifi_scan");
    cJSON* data = cJSON_Parse(content);
    if(data) cJSON_AddItemToObject(root, "data", data);
    else     cJSON_AddStringToObject(root, "data", content);

    char* msg = cJSON_PrintUnformatted(root);
    ai_agent_send(app, msg);
    free(msg);
    cJSON_Delete(root);
}

static void ai_analyze_last_nfc(AIAgentApp* app) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);

    const char* nfc_dir = "/ext/nfc";
    FileInfo info;
    char filepath[256];
    char newest[256] = {0};
    uint64_t newest_ts = 0;

    if(storage_dir_open(f, nfc_dir)) {
        char fname[128];
        while(storage_dir_read(f, &info, fname, sizeof(fname))) {
            if(!(info.flags & FSF_DIRECTORY) && strstr(fname, ".nfc")) {
                snprintf(filepath, sizeof(filepath), "%s/%s", nfc_dir, fname);
                if(info.modified_date > newest_ts) {
                    newest_ts = info.modified_date;
                    snprintf(newest, sizeof(newest), "%s", filepath);
                }
            }
        }
        storage_dir_close(f);
    }

    char content[2048] = {0};
    if(strlen(newest) && storage_file_open(f, newest, FSAM_READ, FSOM_OPEN_EXISTING)) {
        storage_file_read(f, content, sizeof(content) - 1);
        storage_file_close(f);
    } else {
        snprintf(content, sizeof(content), "No NFC capture found.");
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "nfc_dump");
    cJSON* data = cJSON_CreateObject();
    cJSON_AddStringToObject(data, "file", newest);
    cJSON_AddStringToObject(data, "raw", content);
    cJSON_AddItemToObject(root, "data", data);

    char* msg = cJSON_PrintUnformatted(root);
    ai_agent_send(app, msg);
    free(msg);
    cJSON_Delete(root);
}

static void ai_request_bruteforce(AIAgentApp* app) {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "command");
    cJSON_AddStringToObject(root, "cmd",  "generate_bruteforce");
    cJSON_AddStringToObject(root, "protocol", "OOK");
    cJSON_AddNumberToObject(root, "bits", 24);

    char* msg = cJSON_PrintUnformatted(root);
    ai_agent_send(app, msg);
    free(msg);
    cJSON_Delete(root);
}

/* ─── Menu callbacks ─────────────────────────────────────── */

static void menu_callback(void* ctx, uint32_t index) {
    AIAgentApp* app = (AIAgentApp*)ctx;

    switch((MenuIndex)index) {
    case MenuAnalyzeLastRF:
        if(!app->connected) {
            snprintf(app->result_text, RESULT_MAX, "Not connected.\nGo to Settings first.");
        } else {
            snprintf(app->result_text, RESULT_MAX, "Sending to Claude AI...");
            ai_analyze_last_rf(app);
        }
        break;

    case MenuAnalyzeLastWiFi:
        if(!app->connected) {
            snprintf(app->result_text, RESULT_MAX, "Not connected.\nGo to Settings first.");
        } else {
            snprintf(app->result_text, RESULT_MAX, "Sending WiFi scan to AI...");
            ai_analyze_last_wifi(app);
        }
        break;

    case MenuAnalyzeLastNFC:
        if(!app->connected) {
            snprintf(app->result_text, RESULT_MAX, "Not connected.\nGo to Settings first.");
        } else {
            snprintf(app->result_text, RESULT_MAX, "Sending NFC dump to AI...");
            ai_analyze_last_nfc(app);
        }
        break;

    case MenuBruteforce:
        if(!app->connected) {
            snprintf(app->result_text, RESULT_MAX, "Not connected.");
        } else {
            snprintf(app->result_text, RESULT_MAX, "Requesting smart codes...");
            ai_request_bruteforce(app);
        }
        break;

    case MenuSettings:
        /* Switch to text input for server IP */
        view_dispatcher_switch_to_view(app->view_dispatcher, 3);
        return;

    case MenuDisconnect:
        ai_agent_ws_disconnect(app);
        snprintf(app->result_text, RESULT_MAX, "Disconnected from AI server.");
        break;

    default:
        break;
    }

    popup_set_text(app->popup, app->result_text, 64, 32, AlignCenter, AlignCenter);
    view_dispatcher_switch_to_view(app->view_dispatcher, 1);
}

static uint32_t popup_back_callback(void* ctx) {
    UNUSED(ctx);
    return 0; /* return to menu view */
}

static void text_input_callback(void* ctx) {
    AIAgentApp* app = (AIAgentApp*)ctx;
    ai_agent_save_settings(app);
    snprintf(app->server_uri, sizeof(app->server_uri), "ws://%s:8765", app->server_ip);

    /* Connect */
    snprintf(app->result_text, RESULT_MAX, "Connecting to\n%s...", app->server_ip);
    popup_set_text(app->popup, app->result_text, 64, 32, AlignCenter, AlignCenter);
    view_dispatcher_switch_to_view(app->view_dispatcher, 1);

    ai_agent_ws_connect(app);

    /* Wait briefly for connection */
    furi_delay_ms(1500);
    if(app->connected) {
        snprintf(app->result_text, RESULT_MAX, "Connected!\nClaude AI ready.");
    } else {
        snprintf(app->result_text, RESULT_MAX, "Connection failed.\nCheck IP and server.");
    }
    popup_set_text(app->popup, app->result_text, 64, 32, AlignCenter, AlignCenter);
}

/* ─── App lifecycle ──────────────────────────────────────── */

static AIAgentApp* ai_agent_app_alloc(void) {
    AIAgentApp* app = malloc(sizeof(AIAgentApp));
    memset(app, 0, sizeof(AIAgentApp));

    app->gui = furi_record_open(RECORD_GUI);
    app->view_dispatcher = view_dispatcher_alloc();
    view_dispatcher_enable_queue(app->view_dispatcher);
    view_dispatcher_attach_to_gui(app->view_dispatcher, app->gui, ViewDispatcherTypeFullscreen);

    /* Menu view (id=0) */
    app->menu = submenu_alloc();
    submenu_add_item(app->menu, "Analyze Last RF",   MenuAnalyzeLastRF,   menu_callback, app);
    submenu_add_item(app->menu, "Analyze Last WiFi", MenuAnalyzeLastWiFi, menu_callback, app);
    submenu_add_item(app->menu, "Analyze Last NFC",  MenuAnalyzeLastNFC,  menu_callback, app);
    submenu_add_item(app->menu, "Smart Bruteforce",  MenuBruteforce,      menu_callback, app);
    submenu_add_item(app->menu, "Settings / Connect",MenuSettings,        menu_callback, app);
    submenu_add_item(app->menu, "Disconnect",        MenuDisconnect,      menu_callback, app);

    view_dispatcher_add_view(app->view_dispatcher, 0, submenu_get_view(app->menu));

    /* Popup view (id=1) - shows AI results */
    app->popup = popup_alloc();
    popup_set_header(app->popup, "AI Agent", 64, 8, AlignCenter, AlignTop);
    popup_set_callback(app->popup, popup_back_callback);
    popup_set_context(app->popup, app);
    view_dispatcher_add_view(app->view_dispatcher, 1, popup_get_view(app->popup));

    /* Loading view (id=2) */
    app->loading = loading_alloc();
    view_dispatcher_add_view(app->view_dispatcher, 2, loading_get_view(app->loading));

    /* Text input for IP (id=3) */
    app->text_input = text_input_alloc();
    text_input_set_header_text(app->text_input, "Server IP:");
    text_input_set_result_callback(app->text_input, text_input_callback, app,
                                    app->server_ip, sizeof(app->server_ip), true);
    view_dispatcher_add_view(app->view_dispatcher, 3, text_input_get_view(app->text_input));

    ai_agent_load_settings(app);

    return app;
}

static void ai_agent_app_free(AIAgentApp* app) {
    ai_agent_ws_disconnect(app);

    view_dispatcher_remove_view(app->view_dispatcher, 0);
    view_dispatcher_remove_view(app->view_dispatcher, 1);
    view_dispatcher_remove_view(app->view_dispatcher, 2);
    view_dispatcher_remove_view(app->view_dispatcher, 3);

    submenu_free(app->menu);
    popup_free(app->popup);
    loading_free(app->loading);
    text_input_free(app->text_input);
    view_dispatcher_free(app->view_dispatcher);

    furi_record_close(RECORD_GUI);
    free(app);
}

int32_t ai_agent_app(void* p) {
    UNUSED(p);
    AIAgentApp* app = ai_agent_app_alloc();

    view_dispatcher_switch_to_view(app->view_dispatcher, 0);
    view_dispatcher_run(app->view_dispatcher);

    ai_agent_app_free(app);
    return 0;
}
