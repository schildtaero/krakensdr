# Embedded KrakenRF sources

These are the complete tracked source trees at the commits recorded in
`SOURCE_MANIFEST.json`. They are retained as corresponding source alongside the
application package; upstream source files and copyright/license notices have
not been rewritten. No Git metadata, downloaded Python environment or compiled
binary is included.

- `heimdall`: KrakenRF coherent acquisition, GPL-3.0-or-later, `LICENSE`.
- `librtlsdr`: KrakenRF's RTL-SDR fork; its core is GPL-2.0-or-later and some
  components are GPL-3.0-or-later. See `COPYING` and individual file headers.

The installer copies Heimdall to a private runtime build tree and applies the
reviewable `patches/heimdall-unity.patch` plus `krakendf_unity.h`. That build
requires full-rate operation (`decimation_ratio=1`, `fir_tap_size=1`); it uses
upstream's unchanged uint8-to-complex-float conversion and deliberately rejects
other decimation modes. RF filtering is performed coherently in the app. This
removes the KFR/NE10 build dependency for this specific full-rate setup; it is
not an alternative general-purpose decimator.

Native tools run as separate processes. Their source, patches and build recipe
are shipped here so users can rebuild or modify the included GPL components.
The normal app does not execute upstream's root/kill-all startup scripts.
