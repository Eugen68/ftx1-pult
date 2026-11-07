/*
 * _ftx — обёртка Python над ft8_lib (Karlis Goba, MIT): декодирование и кодирование FT8/FT4.
 *
 *   decode(samples: bytes float32 @12000 Гц, ft4: bool, f_min, f_max) -> [(snr, dt, freq, text), ...]
 *   encode(text: str, ft4: bool) -> bytes тонов (79 для FT8, 105 для FT4) | None, если текст не кодируется
 *
 * Синтез звука (GFSK) — в Python. Таблица хэшей позывных — общая между вызовами (как в WSJT-X),
 * чтобы сообщения с <...> разворачивались в полные позывные.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "ft8/decode.h"
#include "ft8/encode.h"
#include "ft8/message.h"
#include "common/monitor.h"

#define MAX_CAND 200
#define MAX_DECODED 80
#define HT_SIZE 512

static struct {
    char callsign[12];
    uint32_t hash;   /* 8 старших бит — «возраст», 22 младших — хэш */
} ht[HT_SIZE];

static void ht_add(const char* callsign, uint32_t hash)
{
    uint16_t h10 = (hash >> 12) & 0x3FFu;
    int i = (h10 * 23) % HT_SIZE;
    int guard = 0;
    while (ht[i].callsign[0] != '\0' && guard++ < HT_SIZE) {
        if ((ht[i].hash & 0x3FFFFFu) == hash && strcmp(ht[i].callsign, callsign) == 0) {
            ht[i].hash &= 0x3FFFFFu;
            return;
        }
        i = (i + 1) % HT_SIZE;
    }
    if (guard >= HT_SIZE) return;
    strncpy(ht[i].callsign, callsign, 11);
    ht[i].callsign[11] = '\0';
    ht[i].hash = hash;
}

static bool ht_lookup(ftx_callsign_hash_type_t type, uint32_t hash, char* callsign)
{
    uint8_t shift = (type == FTX_CALLSIGN_HASH_10_BITS) ? 12 : (type == FTX_CALLSIGN_HASH_12_BITS ? 10 : 0);
    uint16_t h10 = (hash >> (12 - shift)) & 0x3FFu;
    int i = (h10 * 23) % HT_SIZE;
    int guard = 0;
    while (ht[i].callsign[0] != '\0' && guard++ < HT_SIZE) {
        if (((ht[i].hash & 0x3FFFFFu) >> shift) == hash) {
            strcpy(callsign, ht[i].callsign);
            return true;
        }
        i = (i + 1) % HT_SIZE;
    }
    callsign[0] = '\0';
    return false;
}

static void ht_age(uint8_t max_age)
{
    for (int i = 0; i < HT_SIZE; ++i) {
        if (ht[i].callsign[0] == '\0') continue;
        uint8_t age = (uint8_t)(ht[i].hash >> 24);
        if (age > max_age) {
            ht[i].callsign[0] = '\0';
            ht[i].hash = 0;
        } else {
            ht[i].hash = (((uint32_t)age + 1u) << 24) | (ht[i].hash & 0x3FFFFFu);
        }
    }
}

static ftx_callsign_hash_interface_t hash_if = { ht_lookup, ht_add };

/* Отношение сигнал/шум в полосе 2500 Гц (как в WSJT-X), по водопаду: средняя мощность на тонах
   принятого сообщения против медианы водопада. */
static float estimate_snr(const ftx_waterfall_t* wf, const ftx_candidate_t* cand, const uint8_t* tones, int n_tones,
                          float noise_db)
{
    float sum = 0;
    int n = 0;
    for (int k = 0; k < n_tones; ++k) {
        int block = cand->time_offset + k;
        if (block < 0 || block >= wf->num_blocks) continue;
        int idx = block * wf->block_stride + cand->time_sub * wf->freq_osr * wf->num_bins
                  + cand->freq_sub * wf->num_bins + cand->freq_offset + tones[k];
        if (cand->freq_offset + tones[k] >= wf->num_bins) continue;
        float db = WF_ELEM_MAG(wf->mag[idx]);
        sum += powf(10.0f, db / 10.0f);
        n++;
    }
    if (!n) return -30;
    float sig_db = 10.0f * log10f(sum / n);
    float snr = sig_db - noise_db - 26.0f; /* 10*log10(2500 / 6.25) */
    if (snr < -30) snr = -30;
    if (snr > 40) snr = 40;
    return snr;
}

static int cmp_u8(const void* a, const void* b) { return (int)*(const uint8_t*)a - (int)*(const uint8_t*)b; }

static PyObject* py_decode(PyObject* self, PyObject* args)
{
    Py_buffer buf;
    int ft4 = 0;
    float f_min = 200, f_max = 3000;
    int max_cand = 140, ldpc_iter = 25, min_score = 10;
    if (!PyArg_ParseTuple(args, "y*|pffiii", &buf, &ft4, &f_min, &f_max, &max_cand, &ldpc_iter, &min_score))
        return NULL;
    if (max_cand > MAX_CAND) max_cand = MAX_CAND;
    const float* signal = (const float*)buf.buf;
    int num_samples = (int)(buf.len / sizeof(float));

    monitor_t mon;
    monitor_config_t cfg;
    cfg.f_min = f_min;
    cfg.f_max = f_max;
    cfg.sample_rate = 12000;
    cfg.time_osr = 2;
    cfg.freq_osr = 2;
    cfg.protocol = ft4 ? FTX_PROTOCOL_FT4 : FTX_PROTOCOL_FT8;

    PyObject* out = PyList_New(0);
    Py_BEGIN_ALLOW_THREADS
    monitor_init(&mon, &cfg);
    for (int pos = 0; pos + mon.block_size <= num_samples; pos += mon.block_size)
        monitor_process(&mon, signal + pos);
    Py_END_ALLOW_THREADS

    const ftx_waterfall_t* wf = &mon.wf;
    /* уровень шума — медиана водопада */
    float noise_db = -100;
    {
        int total = wf->num_blocks * wf->block_stride;
        if (total > 0) {
            int step = total > 200000 ? total / 200000 : 1;
            int m = total / step;
            uint8_t* tmp = (uint8_t*)malloc(m);
            if (tmp) {
                for (int i = 0; i < m; ++i) tmp[i] = (uint8_t)WF_ELEM_MAG_INT(wf->mag[i * step]);
                qsort(tmp, m, 1, cmp_u8);
                noise_db = tmp[m / 2] * 0.5f - 120.0f;
                free(tmp);
            }
        }
    }

    ftx_candidate_t cands[MAX_CAND];
    int num_cand = ftx_find_candidates(wf, max_cand, cands, min_score);
    ftx_message_t decoded[MAX_DECODED];
    ftx_message_t* table[MAX_DECODED];
    for (int i = 0; i < MAX_DECODED; ++i) table[i] = NULL;
    int n_dec = 0;
    float symbol_period = ft4 ? FT4_SYMBOL_PERIOD : FT8_SYMBOL_PERIOD;

    for (int c = 0; c < num_cand && n_dec < MAX_DECODED; ++c) {
        const ftx_candidate_t* cand = &cands[c];
        ftx_message_t msg;
        ftx_decode_status_t st;
        if (!ftx_decode_candidate(wf, cand, ldpc_iter, &msg, &st)) continue;
        int idx = msg.hash % MAX_DECODED, dup = 0, guard = 0;
        while (table[idx] != NULL && guard++ < MAX_DECODED) {
            if (table[idx]->hash == msg.hash && memcmp(table[idx]->payload, msg.payload, sizeof(msg.payload)) == 0) {
                dup = 1;
                break;
            }
            idx = (idx + 1) % MAX_DECODED;
        }
        if (dup || table[idx] != NULL) continue;
        decoded[idx] = msg;
        table[idx] = &decoded[idx];
        n_dec++;

        char text[FTX_MAX_MESSAGE_LENGTH + 4];
        ftx_message_offsets_t offs;
        if (ftx_message_decode(&msg, &hash_if, text, &offs) != FTX_MESSAGE_RC_OK) continue;

        uint8_t tones[FT4_NN > FT8_NN ? FT4_NN : FT8_NN];
        int nt;
        if (ft4) { ft4_encode(msg.payload, tones); nt = FT4_NN; }
        else { ft8_encode(msg.payload, tones); nt = FT8_NN; }
        float freq = (mon.min_bin + cand->freq_offset + (float)cand->freq_sub / wf->freq_osr) / mon.symbol_period;
        float dt = (cand->time_offset + (float)cand->time_sub / wf->time_osr) * symbol_period;
        float snr = estimate_snr(wf, cand, tones, nt, noise_db);
        PyObject* item = Py_BuildValue("(fffs)", snr, dt, freq, text);
        if (item) {
            PyList_Append(out, item);
            Py_DECREF(item);
        }
    }
    ht_age(10);
    monitor_free(&mon);
    PyBuffer_Release(&buf);
    return out;
}

static PyObject* py_encode(PyObject* self, PyObject* args)
{
    const char* text;
    int ft4 = 0;
    if (!PyArg_ParseTuple(args, "s|p", &text, &ft4)) return NULL;
    ftx_message_t msg;
    ftx_message_init(&msg);
    if (ftx_message_encode(&msg, &hash_if, text) != FTX_MESSAGE_RC_OK) Py_RETURN_NONE;
    uint8_t tones[FT4_NN > FT8_NN ? FT4_NN : FT8_NN];
    int nt;
    if (ft4) { ft4_encode(msg.payload, tones); nt = FT4_NN; }
    else { ft8_encode(msg.payload, tones); nt = FT8_NN; }
    return PyBytes_FromStringAndSize((const char*)tones, nt);
}

static PyObject* py_remember(PyObject* self, PyObject* args)
{
    /* Запомнить позывной в таблице хэшей (свой позывной — чтобы <...> с ним разворачивались) */
    const char* text;
    if (!PyArg_ParseTuple(args, "s", &text)) return NULL;
    ftx_message_t msg;
    ftx_message_init(&msg);
    char buf[64];
    snprintf(buf, sizeof(buf), "CQ %s", text);
    ftx_message_encode(&msg, &hash_if, buf);
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"decode", py_decode, METH_VARARGS, "decode(samples_f32_12k, ft4=False, f_min=200, f_max=3000) -> list"},
    {"encode", py_encode, METH_VARARGS, "encode(text, ft4=False) -> tones bytes or None"},
    {"remember", py_remember, METH_VARARGS, "remember(callsign)"},
    {NULL, NULL, 0, NULL}};

static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_ftx", "FT8/FT4 (ft8_lib)", -1, methods};

PyMODINIT_FUNC PyInit__ftx(void) { return PyModule_Create(&module); }
