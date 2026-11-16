/* KrakenDF full-rate Heimdall build adapter, 2026-11-12.
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This intentionally does NOT implement an FIR library. The patched main()
 * rejects any decimation_ratio/fir_tap_size other than 1 before allocation.
 * Upstream's original uint8-to-float DATA/CAL path remains unchanged. The
 * application's coherent channelizer performs all actual RF filtering.
 */
#ifndef KRAKENDF_UNITY_H
#define KRAKENDF_UNITY_H
#include <stdlib.h>
typedef float kfr_f32;
typedef void KFR_FILTER_F32;
#define kfr_allocate(bytes) malloc(bytes)
#define kfr_filter_create_fir_plan_f32(coeffs, count) ((KFR_FILTER_F32 *)0)
/* A programming/configuration error must never silently skip a filter. */
#define kfr_filter_process_f32(plan, output, input, count) abort()
#endif
