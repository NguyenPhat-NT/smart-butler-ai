/*
 * Bo loc DSP am thanh thoi gian thuc cho loa toan day 4cm ESP32 (24kHz PCM16 mono):
 * 1) High-Pass Butterworth bac 2 (mac dinh 60Hz cho thung loa thong hoi ~4cm)
 * 2) Low-Shelf Bass Boost (~135Hz)
 * 3) High-Shelf Treble Boost (~5200Hz)
 * 4) Soft-knee peak limiter chong meo clip khi boost o am luong thap
 */
#include <math.h>
#include <stdint.h>
#include <stddef.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

typedef struct {
    double b0, b1, b2, a1, a2;
    double z1, z2;
} Biquad;

typedef struct {
    int sample_rate;
    int enabled;
    int norm_enabled;
    double track_gain;
    Biquad hp;
    Biquad low_shelf;
    Biquad high_shelf;
} DspState;

static void biquad_identity(Biquad* q) {
    q->b0 = 1.0;
    q->b1 = 0.0;
    q->b2 = 0.0;
    q->a1 = 0.0;
    q->a2 = 0.0;
}

static void biquad_reset(Biquad* q) {
    q->z1 = 0.0;
    q->z2 = 0.0;
}

static void biquad_highpass(Biquad* q, double fs, double f0, double Q) {
    if (f0 <= 1.0 || f0 >= fs * 0.45) {
        biquad_identity(q);
        return;
    }
    double w0 = 2.0 * M_PI * f0 / fs;
    double cw = cos(w0);
    double alpha = sin(w0) / (2.0 * Q);
    double a0 = 1.0 + alpha;
    q->b0 = ((1.0 + cw) * 0.5) / a0;
    q->b1 = (-(1.0 + cw)) / a0;
    q->b2 = ((1.0 + cw) * 0.5) / a0;
    q->a1 = (-2.0 * cw) / a0;
    q->a2 = (1.0 - alpha) / a0;
}

static void biquad_low_shelf(Biquad* q, double fs, double f0, double gain_db, double S) {
    if (fabs(gain_db) < 0.05 || f0 <= 1.0 || f0 >= fs * 0.45) {
        biquad_identity(q);
        return;
    }
    double A = pow(10.0, gain_db / 40.0);
    double w0 = 2.0 * M_PI * f0 / fs;
    double cw = cos(w0);
    double sw = sin(w0);
    double inner = (A + 1.0 / A) * (1.0 / S - 1.0) + 2.0;
    double alpha = (sw * 0.5) * sqrt(inner > 0.0 ? inner : 0.0);
    double sq = 2.0 * sqrt(A) * alpha;

    double a0 = (A + 1.0) + (A - 1.0) * cw + sq;
    q->b0 = (A * ((A + 1.0) - (A - 1.0) * cw + sq)) / a0;
    q->b1 = (2.0 * A * ((A - 1.0) - (A + 1.0) * cw)) / a0;
    q->b2 = (A * ((A + 1.0) - (A - 1.0) * cw - sq)) / a0;
    q->a1 = (-2.0 * ((A - 1.0) + (A + 1.0) * cw)) / a0;
    q->a2 = ((A + 1.0) + (A - 1.0) * cw - sq) / a0;
}

static void biquad_high_shelf(Biquad* q, double fs, double f0, double gain_db, double S) {
    if (fabs(gain_db) < 0.05 || f0 <= 1.0 || f0 >= fs * 0.45) {
        biquad_identity(q);
        return;
    }
    double A = pow(10.0, gain_db / 40.0);
    double w0 = 2.0 * M_PI * f0 / fs;
    double cw = cos(w0);
    double sw = sin(w0);
    double inner = (A + 1.0 / A) * (1.0 / S - 1.0) + 2.0;
    double alpha = (sw * 0.5) * sqrt(inner > 0.0 ? inner : 0.0);
    double sq = 2.0 * sqrt(A) * alpha;

    double a0 = (A + 1.0) - (A - 1.0) * cw + sq;
    q->b0 = (A * ((A + 1.0) + (A - 1.0) * cw + sq)) / a0;
    q->b1 = (-2.0 * A * ((A - 1.0) + (A + 1.0) * cw)) / a0;
    q->b2 = (A * ((A + 1.0) + (A - 1.0) * cw - sq)) / a0;
    q->a1 = (2.0 * ((A - 1.0) - (A + 1.0) * cw)) / a0;
    q->a2 = ((A + 1.0) - (A - 1.0) * cw - sq) / a0;
}

static inline double biquad_tick(Biquad* q, double x) {
    double y = q->b0 * x + q->z1;
    q->z1 = q->b1 * x - q->a1 * y + q->z2;
    q->z2 = q->b2 * x - q->a2 * y;
    return y;
}

void dsp_configure(
    DspState* st,
    int sample_rate,
    int enabled,
    double highpass_hz,
    double bass_db,
    double treble_db
) {
    if (!st) return;
    st->sample_rate = sample_rate > 8000 ? sample_rate : 24000;
    st->enabled = enabled ? 1 : 0;
    if (st->track_gain < 0.20 || st->track_gain > 4.00) {
        st->track_gain = 1.0;
    }
    double fs = (double)st->sample_rate;
    biquad_highpass(&st->hp, fs, highpass_hz, 0.7071067811865476);
    biquad_low_shelf(&st->low_shelf, fs, 135.0, bass_db, 0.75);
    biquad_high_shelf(&st->high_shelf, fs, 5200.0, treble_db, 0.75);
}

void dsp_set_track_norm(
    DspState* st,
    int norm_enabled,
    double track_gain
) {
    if (!st) return;
    st->norm_enabled = norm_enabled ? 1 : 0;
    if (track_gain >= 0.20 && track_gain <= 4.00) {
        st->track_gain = track_gain;
    } else {
        st->track_gain = 1.0;
    }
}

void dsp_reset(DspState* st) {
    if (!st) return;
    biquad_reset(&st->hp);
    biquad_reset(&st->low_shelf);
    biquad_reset(&st->high_shelf);
}

void dsp_process_s16(DspState* st, int16_t* samples, int count) {
    if (!st || !samples || count <= 0) return;
    if (!st->enabled && !st->norm_enabled) return;
    const double inv = 1.0 / 32768.0;
    const double knee = 0.78;
    const double span = 0.21;
    const int do_eq = st->enabled ? 1 : 0;
    const int do_norm = st->norm_enabled ? 1 : 0;
    const double tg = (do_norm && st->track_gain > 0.05) ? st->track_gain : 1.0;

    for (int i = 0; i < count; ++i) {
        double x = (double)samples[i] * inv;
        if (do_norm) {
            x *= tg;
        }
        if (do_eq) {
            x = biquad_tick(&st->hp, x);
            x = biquad_tick(&st->low_shelf, x);
            x = biquad_tick(&st->high_shelf, x);
        }
        if (x > knee) {
            x = knee + span * tanh((x - knee) / span);
        } else if (x < -knee) {
            x = -knee - span * tanh((-x - knee) / span);
        }
        long v = lrint(x * 32767.0);
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        samples[i] = (int16_t)v;
    }
}

void dsp_analyze_block_s16(
    Biquad* hp,
    const int16_t* samples,
    int count,
    double* out_mean_sq,
    double* out_peak
) {
    if (!samples || count <= 0) {
        if (out_mean_sq) *out_mean_sq = 0.0;
        if (out_peak) *out_peak = 0.0;
        return;
    }
    const double inv = 1.0 / 32768.0;
    double sum_sq = 0.0;
    double pk = 0.0;
    for (int i = 0; i < count; ++i) {
        double x = (double)samples[i] * inv;
        double ax = fabs(x);
        if (ax > pk) pk = ax;
        if (hp) x = biquad_tick(hp, x);
        sum_sq += x * x;
    }
    if (out_mean_sq) *out_mean_sq = sum_sq / (double)count;
    if (out_peak) *out_peak = pk;
}

void dsp_scale_s16(const int16_t* in_samples, int16_t* out_samples, int count, double gain) {
    if (!in_samples || !out_samples || count <= 0) return;
    for (int i = 0; i < count; ++i) {
        long v = lrint((double)in_samples[i] * gain);
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        out_samples[i] = (int16_t)v;
    }
}

void dsp_mix_s16(
    const int16_t* a,
    double gain_a,
    const int16_t* b,
    double gain_b,
    int16_t* out_samples,
    int count
) {
    if (!out_samples || count <= 0) return;
    for (int i = 0; i < count; ++i) {
        double sum = 0.0;
        if (a) sum += (double)a[i] * gain_a;
        if (b) sum += (double)b[i] * gain_b;
        long v = lrint(sum);
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        out_samples[i] = (int16_t)v;
    }
}
