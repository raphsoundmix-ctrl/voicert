# MicCheck — does FMOD actually hear the microphone?

A standalone console app that answers that question **without Unity**, using the same
FMOD build the game ships with. When the voice loop is silent, this separates the
three causes that look identical from inside the game:

1. the microphone is fine and the game picked the wrong device,
2. the device is right but something else owns it,
3. the device opens, reports healthy, and carries digital silence.

## Build

```
dotnet build -c Release
```

The FMOD C# binding is compiled straight out of the FMOD for Unity install rather
than vendored here, so this tool can never drift from the version the game runs.
`fmodstudio.dll` is copied next to the executable automatically. If FMOD lives
somewhere other than the sibling Unity project:

```
dotnet build -c Release -p:FmodUnityDir="C:\Path\To\Assets\Plugins\FMOD"
```

## Use

PowerShell (note: PowerShell 5.1 has no `&&`; use `;` to chain):

```powershell
& ".\bin\Release\net8.0\MicCheck.exe" --list
& ".\bin\Release\net8.0\MicCheck.exe" --device "Microphone (Realtek" --seconds 8
```

| flag | meaning |
|---|---|
| `--list` | enumerate record drivers and exit |
| `--device <index\|substring>` | which input to record; omitted = best-ranked real microphone |
| `--seconds N` | how long to listen (default 8) |
| `--monitor 0..1` | bus volume for audible monitoring. **0 = silent**, the default |
| `--bus <path>` | Studio bus to route into (default `bus:/VoiceInput`) |
| `--output <index\|substring>` | force an output driver instead of walking them |

## Reading the output

```
raw   -38.1 | ch-fader   -38.1 | bus   -52.7 dBFS   playing=True aud=0.000 vol=1.00
```

- **raw** — peak of the PCM this tool captured itself. "The bytes are not all zero."
- **ch-fader** — FMOD's own metering DSP, read **pre-fader**. "The audio engine sees signal."
- **bus** — level arriving at the Studio bus. Non-zero proves the microphone reached the
  mixer strip, not merely the channel in front of it.
- **aud** — audibility. `0.000` means nothing is going to the speakers, so there is no
  feedback risk even while the meters are moving.

Disagreement between `raw` and `ch-fader` is itself the diagnosis: `raw` alive with
`ch-fader` dead means the capture works but the routing into FMOD does not.

## Things this tool established (all measured, not assumed)

- **Loopbacks must never be offered as microphones.** Windows lists `Speakers (X)
  [loopback]` next to real capsules, and `"Realtek USB Audio"` matches *both* the
  microphone and the digital-output tap. Recording the tap captures the game's own
  output and feeds it to speech recognition.
- **The Windows default capture device is regularly not a microphone.** On this machine
  it was `Microphone (Mixing Driver 1 for US-1x2)`, a virtual endpoint that recorded
  72000 frames at a peak of exactly zero while the physical capsule sat beside it.
- **`ERR_RECORD` means another process owns the endpoint**, not that the device is
  broken. Here it was the Tascam control panel (`us1x2mixer` / `us1x2mxsub`) and FMOD
  Studio holding the US-1x2.
- **Do not silence the monitor with `Channel.setVolume(0)`.** Channel volume is applied
  by the channel's FADER DSP, so zero reads zero at that fader's output and drives
  audibility to zero, making the channel the first candidate for voice stealing — and a
  stolen channel runs no DSP, so the meter dies with no error. Measured: channel at 0
  read −120 dBFS where the same microphone read −45.6 dBFS at unity. Hold the channel at
  unity and attenuate the **bus**.
- **`Bus.lockChannelGroup()` is asynchronous.** The core ChannelGroup behind a bus does
  not exist until the Studio command queue has been flushed. Calling `getChannelGroup()`
  before `flushCommands()` fails intermittently, which is worse than failing always.
  Always `unlockChannelGroup()` on teardown, or the strip is pinned for the process life.
- **FMOD for Unity ships one Windows library containing both APIs.** The binding imports
  them as `fmod` and `fmodstudio`; satisfying each with its own copy of the file loads it
  twice and every core call lands in the instance that does not own the handles. The
  symptom is a core system with `hasHandle == true` that answers `ERR_INVALID_HANDLE` to
  everything and reports version `0x00000000`. `MicCheck.cs` installs a DllImport
  resolver pointing both names at one file.
