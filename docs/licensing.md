# Licensing will bite you before performance does
<a id="licensing-will-bite-you-before-performance-does"></a>

The engine most people reach for first will not survive legal review.

**Piper** now lives at `OHF-Voice/piper1-gpl` and is **GPL-3.0**. The old MIT `rhasspy/piper` was archived read-only on 2025-10-06. Piper embeds espeak-ng, which is GPL, so linking against it makes your binary a derived work that has to ship under GPL-3.0 too. Dynamic linking does not get you out of that. Pinning the archived MIT code does not help either, because its CMake still pulls in and links GPL espeak-ng.

Two routes work. Run Piper as a separate process, which is normally treated as mere aggregation, though you still ship a GPL binary alongside your game. Talk to a lawyer about that, and note that the GPLv3 Installation Information requirement is a real problem on locked consoles. Or use an MIT fork with an espeak-free G2P, such as `ayutaz/piper-plus`.

**Voice models are licensed separately from the engine.** Piper's own docs say so, and `en_US-lessac`, the voice in its examples, is under the restrictive Blizzard 2013 license. Read the `MODEL_CARD` of every voice you ship.

**Kokoro-82M is Apache-2.0, weights included** (8 languages, 54 voices, 86–326 MB of ONNX). Clean licensing, but it measured **RTF 2.77–6.63 on a Raspberry Pi 4**, so it is not real time on a low-power ARM chip. Fine on desktop; benchmark it before you commit a Switch-class handheld target.

**sherpa-onnx** (Apache-2.0) is the easiest runtime to ship: Windows, Linux, macOS, Android, iOS, on x86, ARM and RISC-V, with 12 language bindings and no network needed. It still pulls in GPL espeak-ng through piper-phonemize, though there is an open upstream issue to remove it.

**Supertonic** has the fastest CPU numbers here, but its weights are OpenRAIL-M, which restricts use rather than being plainly permissive.

None of this stops you shipping. It means choosing the stack with a lawyer in the room. Because every provider sits behind the same adapter interface, changing that choice later is a swap rather than a rewrite.

The measured on-device numbers these choices feed into are in [local-stack.md](local-stack.md); the cost model that depends on them is in [economics.md](economics.md).
