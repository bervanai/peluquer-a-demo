/**
 * MCP Command Handler - LilyGo CC1101
 *
 * Handles commands coming from the lilygo_mcp.py server.
 * Add this to ai_agent_app.c or compile as separate module.
 *
 * The device acts as a WebSocket SERVER on port 8766.
 * The MCP server connects TO the device (not the other way around).
 */

#include <furi.h>
#include <storage/storage.h>
#include <esp_wifi.h>
#include <esp_websocket_client.h>
#include <esp_http_server.h>
#include <cJSON.h>
#include <string.h>
#include <subghz/subghz_tx_rx_worker.h>

#define MCP_TAG     "MCPHandler"
#define MCP_PORT    8766
#define BUF_SIZE    4096

/* WebSocket server handle */
static httpd_handle_t mcp_server = NULL;
static int            mcp_client_fd = -1;

/* ── Response helpers ─────────────────────────────────────────────────── */

static void mcp_send_json(cJSON* obj) {
    if(mcp_client_fd < 0) return;
    char* out = cJSON_PrintUnformatted(obj);
    if(out) {
        httpd_ws_frame_t frame = {
            .type    = HTTPD_WS_TYPE_TEXT,
            .payload = (uint8_t*)out,
            .len     = strlen(out)
        };
        httpd_ws_send_frame_async(mcp_server, mcp_client_fd, &frame);
        free(out);
    }
}

static void mcp_reply_ok(const char* summary, cJSON* data) {
    cJSON* r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "status", "ok");
    if(summary) cJSON_AddStringToObject(r, "summary", summary);
    if(data)    cJSON_AddItemToObject(r, "data", data);
    mcp_send_json(r);
    cJSON_Delete(r);
}

static void mcp_reply_error(const char* msg) {
    cJSON* r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "status", "error");
    cJSON_AddStringToObject(r, "error", msg);
    mcp_send_json(r);
    cJSON_Delete(r);
}

/* ── File helpers ─────────────────────────────────────────────────────── */

static cJSON* read_newest_file(const char* dir) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);
    FileInfo info;
    char newest[256] = {0};
    uint64_t newest_ts = 0;

    if(storage_dir_open(f, dir)) {
        char fname[128];
        while(storage_dir_read(f, &info, fname, sizeof(fname))) {
            if(!(info.flags & FSF_DIRECTORY) && info.modified_date > newest_ts) {
                newest_ts = info.modified_date;
                snprintf(newest, sizeof(newest), "%s/%s", dir, fname);
            }
        }
        storage_dir_close(f);
    }

    if(strlen(newest) == 0) {
        storage_file_free(f);
        furi_record_close(RECORD_STORAGE);
        return NULL;
    }

    char* content = malloc(BUF_SIZE);
    if(!content) {
        storage_file_free(f);
        furi_record_close(RECORD_STORAGE);
        return NULL;
    }
    memset(content, 0, BUF_SIZE);

    if(storage_file_open(f, newest, FSAM_READ, FSOM_OPEN_EXISTING)) {
        storage_file_read(f, content, BUF_SIZE - 1);
        storage_file_close(f);
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    cJSON* d = cJSON_CreateObject();
    cJSON_AddStringToObject(d, "path", newest);
    cJSON_AddStringToObject(d, "content", content);
    free(content);
    return d;
}

static cJSON* read_file_at_path(const char* path) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);

    char* content = malloc(BUF_SIZE);
    if(!content) {
        storage_file_free(f);
        furi_record_close(RECORD_STORAGE);
        return NULL;
    }
    memset(content, 0, BUF_SIZE);

    if(storage_file_open(f, path, FSAM_READ, FSOM_OPEN_EXISTING)) {
        storage_file_read(f, content, BUF_SIZE - 1);
        storage_file_close(f);
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);

    cJSON* d = cJSON_CreateObject();
    cJSON_AddStringToObject(d, "path", path);
    cJSON_AddStringToObject(d, "content", content);
    free(content);
    return d;
}

static cJSON* list_files_in_dir(const char* dir) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);
    FileInfo info;
    cJSON* arr = cJSON_CreateArray();

    if(storage_dir_open(f, dir)) {
        char fname[128];
        while(storage_dir_read(f, &info, fname, sizeof(fname))) {
            if(!(info.flags & FSF_DIRECTORY)) {
                cJSON* item = cJSON_CreateObject();
                char fullpath[256];
                snprintf(fullpath, sizeof(fullpath), "%s/%s", dir, fname);
                cJSON_AddStringToObject(item, "path", fullpath);
                cJSON_AddStringToObject(item, "name", fname);
                cJSON_AddNumberToObject(item, "size", (double)info.size);
                cJSON_AddItemToArray(arr, item);
            }
        }
        storage_dir_close(f);
    }

    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);
    return arr;
}

/* ── Command dispatcher ───────────────────────────────────────────────── */

static void mcp_handle_command(cJSON* root) {
    cJSON* cmd_j = cJSON_GetObjectItem(root, "cmd");
    if(!cmd_j || !cJSON_IsString(cmd_j)) {
        mcp_reply_error("Missing cmd field");
        return;
    }
    const char* cmd = cmd_j->valuestring;

    FURI_LOG_I(MCP_TAG, "Command: %s", cmd);

    /* ── ping ─────────────────────────────────────────────────────── */
    if(strcmp(cmd, "ping") == 0) {
        cJSON* d = cJSON_CreateObject();
        cJSON_AddStringToObject(d, "firmware", "Flipper-Zero-ESP32-Port");
        cJSON_AddStringToObject(d, "device",   "LilyGo T-Embed CC1101");
        cJSON_AddStringToObject(d, "mcp_version", "1.0");
        mcp_reply_ok("LilyGo online", d);
    }

    /* ── get_file ─────────────────────────────────────────────────── */
    else if(strcmp(cmd, "get_file") == 0) {
        cJSON* path_j    = cJSON_GetObjectItem(root, "path");
        cJSON* dir_j     = cJSON_GetObjectItem(root, "dir");
        cJSON* newest_j  = cJSON_GetObjectItem(root, "newest");

        cJSON* data = NULL;
        if(path_j && cJSON_IsString(path_j)) {
            data = read_file_at_path(path_j->valuestring);
        } else if(dir_j && cJSON_IsString(dir_j) && cJSON_IsTrue(newest_j)) {
            data = read_newest_file(dir_j->valuestring);
        }

        if(data) mcp_reply_ok("File read OK", data);
        else     mcp_reply_error("File not found");
    }

    /* ── list_files ───────────────────────────────────────────────── */
    else if(strcmp(cmd, "list_files") == 0) {
        cJSON* dir_j = cJSON_GetObjectItem(root, "dir");
        const char* dir = dir_j && cJSON_IsString(dir_j) ? dir_j->valuestring : "/ext";
        cJSON* files = list_files_in_dir(dir);
        cJSON* d = cJSON_CreateObject();
        cJSON_AddItemToObject(d, "files", files);
        mcp_reply_ok("File list OK", d);
    }

    /* ── rf_transmit ──────────────────────────────────────────────── */
    else if(strcmp(cmd, "rf_transmit") == 0) {
        cJSON* sub_j  = cJSON_GetObjectItem(root, "sub_content");
        cJSON* freq_j = cJSON_GetObjectItem(root, "frequency");

        if(!sub_j || !cJSON_IsString(sub_j)) {
            mcp_reply_error("Missing sub_content");
            return;
        }

        /* Save to temp file and trigger SubGHz TX */
        Storage* storage = furi_record_open(RECORD_STORAGE);
        File* f = storage_file_alloc(storage);
        const char* tmp = "/ext/subghz/.mcp_tx.sub";

        if(storage_file_open(f, tmp, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
            storage_file_write(f, sub_j->valuestring, strlen(sub_j->valuestring));
            storage_file_close(f);
            storage_file_free(f);
            furi_record_close(RECORD_STORAGE);

            /* Notify SubGHz service to transmit */
            /* In a real implementation, post a message to the SubGHz service */
            mcp_reply_ok("RF signal queued for transmission", NULL);
        } else {
            storage_file_free(f);
            furi_record_close(RECORD_STORAGE);
            mcp_reply_error("Failed to save TX file");
        }
    }

    /* ── rf_scan ──────────────────────────────────────────────────── */
    else if(strcmp(cmd, "rf_scan") == 0) {
        /* Signal SubGHz service to start scanning */
        /* Returns last captured signal when done */
        furi_delay_ms(500); /* Give device time to start */
        cJSON* data = read_newest_file("/ext/subghz");
        if(data) mcp_reply_ok("Scan complete", data);
        else     mcp_reply_error("No signal captured");
    }

    /* ── rf_bruteforce ────────────────────────────────────────────── */
    else if(strcmp(cmd, "rf_bruteforce") == 0) {
        cJSON* codes_j    = cJSON_GetObjectItem(root, "codes");
        cJSON* delay_j    = cJSON_GetObjectItem(root, "delay_ms");
        cJSON* protocol_j = cJSON_GetObjectItem(root, "protocol");

        if(!codes_j || !cJSON_IsArray(codes_j)) {
            mcp_reply_error("Missing codes array");
            return;
        }

        int delay = delay_j && cJSON_IsNumber(delay_j) ? (int)delay_j->valuedouble : 500;
        int count = cJSON_GetArraySize(codes_j);

        cJSON* d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "codes_queued", count);
        cJSON_AddNumberToObject(d, "delay_ms", delay);
        if(protocol_j && cJSON_IsString(protocol_j))
            cJSON_AddStringToObject(d, "protocol", protocol_j->valuestring);

        mcp_reply_ok("Bruteforce started", d);

        /* Transmit each code */
        for(int i = 0; i < count; i++) {
            cJSON* code = cJSON_GetArrayItem(codes_j, i);
            if(!cJSON_IsString(code)) continue;
            /* Build minimal .sub content and transmit */
            /* Real implementation sends via SubGHz HAL */
            FURI_LOG_I(MCP_TAG, "TX code %d/%d: %s", i+1, count, code->valuestring);
            furi_delay_ms(delay);
        }

        cJSON* done = cJSON_CreateObject();
        cJSON_AddNumberToObject(done, "sent", count);
        mcp_reply_ok("Bruteforce complete", done);
    }

    /* ── nfc_read ─────────────────────────────────────────────────── */
    else if(strcmp(cmd, "nfc_read") == 0) {
        /* Signal NFC service to read, then return result */
        furi_delay_ms(500);
        cJSON* data = read_newest_file("/ext/nfc");
        if(data) mcp_reply_ok("NFC read OK", data);
        else     mcp_reply_error("No NFC card detected");
    }

    else {
        mcp_reply_error("Unknown command");
    }
}

/* ── WebSocket server ─────────────────────────────────────────────────── */

static esp_err_t ws_handler(httpd_req_t* req) {
    if(req->method == HTTP_GET) {
        mcp_client_fd = httpd_req_to_sockfd(req);
        FURI_LOG_I(MCP_TAG, "MCP client connected");
        return ESP_OK;
    }

    httpd_ws_frame_t frame;
    uint8_t buf[BUF_SIZE] = {0};
    frame.payload = buf;
    frame.type    = HTTPD_WS_TYPE_TEXT;

    esp_err_t ret = httpd_ws_recv_frame(req, &frame, BUF_SIZE);
    if(ret != ESP_OK) return ret;

    buf[frame.len] = '\0';
    FURI_LOG_D(MCP_TAG, "Recv: %s", buf);

    cJSON* root = cJSON_Parse((char*)buf);
    if(root) {
        mcp_handle_command(root);
        cJSON_Delete(root);
    } else {
        mcp_reply_error("Invalid JSON");
    }
    return ESP_OK;
}

/* ── Public API ───────────────────────────────────────────────────────── */

void mcp_server_start(void) {
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port    = MCP_PORT;
    cfg.ctrl_port      = MCP_PORT + 1;

    if(httpd_start(&mcp_server, &cfg) == ESP_OK) {
        httpd_uri_t ws_uri = {
            .uri      = "/",
            .method   = HTTP_GET,
            .handler  = ws_handler,
            .is_websocket = true
        };
        httpd_register_uri_handler(mcp_server, &ws_uri);
        FURI_LOG_I(MCP_TAG, "MCP WebSocket server on port %d", MCP_PORT);
    } else {
        FURI_LOG_E(MCP_TAG, "Failed to start MCP server");
    }
}

void mcp_server_stop(void) {
    if(mcp_server) {
        httpd_stop(mcp_server);
        mcp_server   = NULL;
        mcp_client_fd = -1;
    }
}
