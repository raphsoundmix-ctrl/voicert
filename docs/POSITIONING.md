# VoiceRT positioning

Status: DRAFT. Raph confirms the sentence in AltaLab Day 1, Task 4. Until then every public sentence on the site, in the README and in the GitHub About is judged against it: one promise, one standard.

## The one sentence

> We help Unity game developers give NPCs real, interruptible voice conversations without a recording budget, through one FMOD-native character asset, so players can talk to any NPC and get an in-character answer inside the game's own mix.

What is true today, and what public copy says next to the sentence: the Unity package streams into a plain `AudioSource`; the FMOD programmer-instrument sink is milestone M1; nothing has been compiled inside a Unity Editor yet. The sentence is the promise; the status line stays beside it until M1 ships.

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

Topics: telephony (SIP, Twilio, μ-law, 8 kHz, −22 dBFS), CRM, personal assistant, IoT, calendar, business owner, "one core, many jobs", "real-time voice agent framework" as the framing, the `sales` and `assistant` profiles.

One permitted mention, in `docs/architecture.md` only: other profiles exist in `ConfigFactory` only to prove tool sets cannot overlap; they are not on the roadmap.

Honesty rules stay. Every caveat about un-compiled engine glue, un-benchmarked phones and prototype status is preserved. Nothing is upgraded from "designed" to "measured" or from "prototype" to "shipped".

## Pitch-deck anchors (AltaLab Day 1 lecture)

| Anchor | Current answer | Status |
|---|---|---|
| MVP | Unity + FMOD NPC asset: UPM package, FMOD programmer-instrument sink, one demo scene (tavern keeper) | DRAFT |
| Key metric | NPC conversations completed per week across installs | ASSUMPTION, decided Day 3 |
| Key growth channel | GitHub, the FMOD forum, the Unity forum, game-audio Discords | ASSUMPTION, decided Day 4 |

## Milestones

| | Milestone | Done when |
|---|---|---|
| M1 | FMOD programmer-instrument sink in the Unity package | compiled in Unity 6.3 LTS with FMOD for Unity 2.03.14; a programmer sound plays PCM from the bridge |
| M2 | Demo scene: tavern keeper | mic → NPC → FMOD event, barge-in and LOD visible; 30-second video recorded |
| M3 | Live providers behind the adapters | Deepgram / Claude Haiku / ElevenLabs Flash and faster-whisper / Ollama / sherpa-onnx both run end to end |
| M4 | UPM 0.1 release | package published; Unreal + Wwise at parity |

M1 target: end of the AltaLab sprint, 2026-09-27.
