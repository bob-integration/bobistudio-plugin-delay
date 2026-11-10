# Delay — Bobi.Studio's N→N delay line

*[Version française](README.md)*

Delays X video and/or audio inputs to X matching outputs. Submodule of
[bobi.studio](https://github.com/bob-integration/bobistudio), mounted at `plugins/delay/`.

## What it does

X delayed inputs to X matching outputs, from 1 to 16 channels. Each channel is independent:
input V1 → output V1, A1 → output A1. Three essence choices at deployment — video only (default),
video + audio, or audio only.

Used to realign sources with different latencies before a mix, or to make up the accumulated
delay of a chain before a comparison point.

The input format is passed through unchanged: the delay converts nothing, and a delay of D frames
is **exact** — no frame lost or duplicated.

## Coupled or decoupled

Coupled (default), a single setting drives picture and sound: sound follows picture, converted
using the measured video period. Decoupled, two independent settings — picture in frames, sound
in milliseconds.

One consequence to know about, intended by decoupling: a recorder placed behind this delay comes
out offset by the **difference** between the two settings, since it pairs frame *k* with the sound
of the same instant. The ISO will carry it.

## Live adjustment

The delay is set without redeploying: a global slider that pushes the same value everywhere, or a
slider per channel. The **linked channels** button sets every channel to the same value, to keep a
group in sync. Values set live are **persisted**: they survive a restart as well as a redeployment
of the container.

Changing the essence or the number of channels, on the other hand, is structural and redeploys.

## Set and measured delay

`GET :8082/state` and `/channels` publish, per channel, the **set** delay (`delay_frames`) and the
**actually measured** delay (`measured_ms`, `realized_frames`), plus the channel's fps. Comparing
the two points to a channel that falls behind, or that does not have the expected source; without
this pair, the applied delay cannot be verified.

A lost source triggers automatic reopening of the MXL flow; no operator action is needed.

## Slice mode

At zero delay, the plugin follows the source band by band as it arrives: no added latency of its
own. At a delay of one frame or more, it republishes the delayed frame **re-sliced** into bands —
downstream keeps the sub-frame benefit through the stage, even though delaying does require
waiting for the whole frame.

Conditions: a progressive source, and a band height that divides the frame height. Otherwise the
plugin says so in its log and falls back to whole frames. Optional, off by default.

## Memory cost

Each channel buffers `max_delay_frames` frames, or the audio equivalent, **in memory**. A high
maximum delay on many high-resolution channels consumes RAM accordingly, and it does so even when
the current delay is zero: the buffer is sized by the bound, not by the setting.

Audio travels in packets whose size comes from the core setting (Settings → MXL, 1 ms by
default); the plugin reads it, it does not decide it.

## Macros and external control

The global delay, a channel's delay, the decoupled audio delay and picture/sound coupling are
exposed to macros and triggers, with a `param_tree` for the continuous settings. Control from a
panel goes through there.

## Licence

GNU GPL v3 or later — see `LICENSE`.
