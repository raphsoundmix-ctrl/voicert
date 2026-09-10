# Wiring it into FMOD and Wwise

The rule is simple: an AI voice has to be an ordinary voice. Same buses, attenuation curves, occlusion, reverb sends, ducking and voice limiting that the sound designer already set up. Anything else means keeping a second mix alive, and that is where AI dialogue starts to sound pasted on.

**Status.** `voicert.game.sinks` defines the contract. The Unity package implements both the plain `AudioSource` path and the FMOD programmer-instrument sink, compiled in Unity 6.3 LTS with FMOD for Unity 2.03.14 and verified live through a real microphone. The Unreal plugin's Wwise glue follows the documented API shape but has not been exercised inside an Editor.

## FMOD (Unity first)

**FMOD** uses a user-created sound. Call `System::createSound` with `FMOD_OPENUSER` and an `FMOD_CREATESOUNDEXINFO` carrying `defaultfrequency`, `numchannels`, `format` and `pcmreadcallback`, and FMOD pulls PCM in `decodebuffersize` blocks. Pass that sound to a programmer instrument through `FMOD_STUDIO_EVENT_CALLBACK_CREATE_PROGRAMMER_SOUND`, release it in the matching DESTROY callback, and the generated voice inherits the event's 3D panning, buses and effects.

## Unity without middleware

**No middleware.** Use Unity's `OnAudioFilterRead` or `AudioClip.Create(..., stream: true, PCMReaderCallback)`, or Unreal's `USoundWaveProcedural::QueueAudio` (push) or `ISoundGenerator::OnGenerateAudio` through `USynthComponent` (pull).

## Wwise (Unreal)

**Wwise** uses the stock Audio Input source plug-in. Register three global callbacks once with `SetAudioInputCallbacks()`. The engine calls the format callback when playback starts and the execute callback on every audio frame, until you stop it or return `AK_NoMoreData`. In Unreal, subclass `UAkAudioInputComponent`, override `FillSamplesBuffer` and `GetChannelConfig`, and start it with **Post Associated Audio Input Event**; a plain PostEvent will not drive the plug-in. The transport is well proven: ReadSpeaker's speechEngine, 4Players ODIN voice chat and Unreal AudioLink all use it.

## Concurrency

Let the middleware handle concurrency. FMOD separates `maxchannels` (virtual voices, usually 256–1024) from `setSoftwareChannels` (real mixed voices, 64 by default). Wwise resolves per-object, per-bus and project-wide playback limits by priority, with Virtual Voice Behavior deciding what happens beyond the cap. Keep the dialogue pool well under those numbers.

## The sink contract

`voicert.game.sinks` defines the contract: `push(pcm, sample_rate)` and `flush()`. That is all the native shim has to implement. `flush()` is the barge-in path and has to drop queued audio immediately, or the NPC carries on talking after the player cut them off.
