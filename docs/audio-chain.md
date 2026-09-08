# Audio chain

A clean input means fewer STT mistakes, fewer wasted tokens, lower latency and lower cost. This chain comes from mixing work, not from documentation defaults.

**Input, microphone to STT:**

```
HPF 80–120 Hz  →  Denoise (RNNoise/Silero)  →  AGC (target −18 dBFS)
   →  Soft Limiter (−3 dBFS)  →  Resample 16 kHz mono  →  VAD (Silero, <1 ms, on-device)
```

The high-pass filter removes rumble, mains hum and the proximity effect of cheap headsets. AGC levels the signal before the VAD sees it, otherwise the detection threshold drifts with how loudly someone speaks. The VAD has to run locally, because barge-in can only be as fast as the moment you notice the user talking.

**Output, TTS to the FMOD bus:**

```
TTS 24–48 kHz  →  De-esser (5–8 kHz)  →  Presence EQ (+1.5 dB @ 3–5 kHz)
   →  Loudness (−16 LUFS game dialogue bus)  →  PCM16 16 kHz → FMOD programmer sound
   (Opus 48k only when the voicert process is on another machine)
```

Synthesis hisses on sibilants, so a de-esser before the bus is not optional. The presence lift keeps speech intelligible under music and ambience, on TV speakers and on a headset. On barge-in the queued PCM is dropped immediately (`flush()` in the sink contract), or the NPC keeps finishing its sentence after the cut. Everything after that point, 3D attenuation, occlusion, reverb sends, ducking, is the game's own mix: see [middleware-wiring.md](middleware-wiring.md).

