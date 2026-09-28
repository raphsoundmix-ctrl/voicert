# VoiceRT positioning

Status: the sentence below is the mission statement from the AltaLab Day 1 practice (2026-09-08). Every public sentence on the site, in the README and in the GitHub About is judged against it: one promise, one standard.

## The one sentence

> We help Unity game developers escape the cost, time, and localization limits of pre-recorded voice-over by shipping one FMOD-native, interruptible AI voice agent, so players can talk to any NPC and hear it answer live, in character, inside the game's own mix.

What is true today, and what public copy says next to the sentence: the Unity package streams into a plain `AudioSource` **or** the FMOD programmer-instrument sink; both are compiled in Unity 6 (6000.6) and the FMOD path is verified live through a real microphone, fully local on one GPU. M1 has shipped. What stays un-exercised, and must keep saying so: the Unreal + Wwise glue. Speech recognition is English-only today (Whisper `small.en`), so "localization" is a direction, not a shipped feature.

## Two surfaces

The site is for investors, accelerators and studio leads, and follows the AltaLab pitch order: why now, the pain, the product, the proof, the market, the model, the team, the ask. The README and `docs/*.md` are for engineers. When a technical section grows on the site, it moves to the repo and the site links to it.

## Audience

**Who.** Unity indie and mid-size studios. Technical sound designers who own the FMOD project. Gameplay programmers who own the NPC.

**Who not.** Voice-AI engineers choosing a framework. Call-centre or sales automation. Personal-assistant products. Anyone whose audio does not go through a game engine.

## Bridge order

1. Unity + FMOD (programmer instrument)
2. Unity, plain `AudioSource` (no middleware)
3. Unreal + Wwise (`UAkAudioInputComponent`)

Why this order: FMOD Studio's Unity integration is the most common middleware path for indie and mid-size Unity teams, and the team's game-audio depth is FMOD (Vyacheslav Romanenko, 14+ years of technical sound design on PC, console and mobile). Every sentence that names both middlewares says "FMOD or Wwise". Every sentence that names both engines says "Unity or Unreal".

## Banned on public surfaces

Words: seamless, revolutionary, leverage, unlock. No emoji in copy.

Topics: telephony (SIP, Twilio, μ-law, 8 kHz, −22 dBFS), CRM, personal assistant, IoT, calendar, business owner, "one core, many jobs", "real-time voice agent framework" as the framing. The old `sales` and `assistant` profiles were deleted from the code on 2026-09-28.

Honesty rules stay. Every caveat about un-compiled engine glue, un-benchmarked phones and prototype status is preserved. Nothing is upgraded from "designed" to "measured" or from "prototype" to "shipped".

## Pitch-deck anchors (AltaLab Day 1 lecture)

| Anchor | Current answer | Status |
|---|---|---|
| MVP | Unity + FMOD NPC asset: UPM package, FMOD programmer-instrument sink, one demo scene (tavern keeper) | M1 shipped; demo scene is M2 |
| Key metric | NPC conversations completed per week across installs | ASSUMPTION, no installs yet |
| Key growth channel | Direct outbound to Unity developers building sandbox and simulation games; metric: 10-minute demo calls booked per week | Chosen in the AltaLab Day 4 practice (2026-09-27) |

## Milestones

| | Milestone | Done when |
|---|---|---|
| M1 | FMOD programmer-instrument sink in the Unity package | compiled in Unity 6 with FMOD for Unity 2.03.14; a programmer sound plays PCM from the bridge (done) |
| M2 | Demo scene: tavern keeper | mic → NPC → FMOD event, barge-in and LOD visible; 30-second video recorded |
| M3 | Live providers behind the adapters | Deepgram / Claude Haiku / ElevenLabs Flash and faster-whisper / Ollama / sherpa-onnx both run end to end |
| M4 | UPM 0.1 release | package published; Unreal + Wwise at parity |

M1 target: end of the AltaLab sprint, 2026-09-27.
