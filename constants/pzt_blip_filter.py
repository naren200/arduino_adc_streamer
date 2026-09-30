"""Defaults for the PZT ADC single-sample blip filter."""

PZT_BLIP_FILTER_DEFAULT_ENABLED = True
PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES = 3
PZT_BLIP_FILTER_MIN_WINDOW_SAMPLES = 3
PZT_BLIP_FILTER_MAX_WINDOW_SAMPLES = 15


def normalize_pzt_blip_filter_window(window_samples: int) -> int:
    """Clamp to the supported range and force odd (a median needs a middle element)."""
    window = int(window_samples)
    if window % 2 == 0:
        window += 1
    return min(
        PZT_BLIP_FILTER_MAX_WINDOW_SAMPLES,
        max(PZT_BLIP_FILTER_MIN_WINDOW_SAMPLES, window),
    )
