# A2_PZT3 trial-start failures, 2026-11-09

Reviewed session `077bc8a4db464ce99f367e86e5561514` without modifying its raw
recordings, sidecars or active calibration choices.

## Why the trial did not arm

The latest recording (`_005`) was checking a previously frozen linear drift
correction. It ended at 39.661 s after a capture gap: 214 sweeps were missing,
approximately 0.355 s at the recorded channel rate. This was not the 120 s
settling timeout, and no placement/press marks had been recorded.

Its most recent complete drift check used 29.238–34.238 s for training and
34.238–39.238 s for checking. Combined slope change was 2.220 pC/s against
an allowance of 1.765 pC/s. Total absolute per-channel mismatch from the saved
rates was 26.644 pC/s. B differed most: its saved rate was -110.402 pC/s,
versus -131.813 pC/s in the later check. The correction could not be verified,
so the trial was still blocked before data loss stopped the capture. These
are measured rest-data differences, not proof of a particular physical cause.

The preceding retry (`_004`) similarly failed drift verification, then stopped
at 35.955 s with CRC/gap errors and 403 missing sweeps. Earlier, `_003` actually
qualified a fresh linear correction at 26.124 s and began its trial baseline;
55 missing sweeps stopped it at 26.284 s, before placement could be armed.

The current files are marked `known_mass`, although the user is testing with a
finger. Workflow-only mode should be selected before the next diagnostic run;
the nominal 20 g row is not a measured finger force or a valid gain reference.

## Host-side work addressed

Each periodic live drift check called the full capture-result snapshot on the
same thread that reads serial data. It recomputed connected/revisit timing
statistics for all 50 routes, even though that check uses only selected sensor
samples and ADC-reference statistics. On the saved 39.7 s recording, a local
benchmark measured approximately 33 ms per full snapshot. It now omits unused
timing calculations, taking approximately 3.8 ms in the same benchmark.
Selected samples, ADC-reference statistics, quality checks and final-recording
statistics are unchanged.

SerialLink now requests a 1 MiB receive queue where the driver supports queue
resizing. Unsupported drivers retain their defaults. This adds capacity for
brief host calculation/painting pauses; the driver may ignore the request.
The raw logs do not uniquely locate the original transport fault, so a hardware
retry is still needed to establish whether these changes eliminate the gaps.
CRC/gap failures continue to invalidate a force capture; no missing increments
are filled or silently integrated.

The live prompt now shows slope-change and saved-rate mismatch values and the
reason for waiting. Final rejection also retains the last drift-check explanation
alongside capture errors. Acceptance budgets, the 120 s maximum wait and the
rule against automatically refitting frozen drift during a trial are unchanged.

## Next run

Restart CapTest and reconnect so the serial queue request takes effect. Select
the package, display sensitivity/sign and workflow-only mode before measuring
drift. Start a fresh **Drift test → first weight trial** to measure the current
quiet section and continue into the trial on the same capture, rather than
checking the stale saved correction again. In an existing calibration context,
Reset trials starts a new run and keeps the original recording files.

If gaps recur, increase Added lag per sweep before a fresh drift measurement;
that reduces the streaming rate and changes the capture schedule. Do not apply
force until the prompt is armed.

Focused validation: 90 link/capture/drift/controller/GUI tests passed, including
equivalence of fast/full quality snapshots, serial queue fallback and live drift
rejection feedback. Full CapTest suite: **554 passed, 13 skipped** (75 existing
pyqtgraph deprecation warnings). The live rejection text was rendered and visually
inspected. The hardware result remains to be confirmed.
