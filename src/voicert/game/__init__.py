"""Game integration layer — the economics of voicing an open world.

The problem this layer solves is not "can an LLM talk". It is: *an open world
has hundreds of NPCs, a studio has one CPU budget and no voice-acting money,
and the frame budget is 16.6 ms.*

Three ideas do the work:

1. **Dialogue LOD** (``DialogueLOD``) — dialogue gets a level-of-detail system,
   exactly like geometry. Only the NPC you are actually talking to runs the
   full pipeline; audible-but-distant NPCs pick pre-baked barks; the crowd is
   an ambient bed. Cost scales with what the player can *hear*, not with how
   many NPCs exist.

2. **A bounded voice pool** (``NPCVoicePool``) — live agents are a fixed-size,
   priority-evicted resource, mirroring how Wwise/FMOD already cap concurrent
   voices. 500 NPCs in the district, 3 agents in memory.

3. **A generated-audio sink** (``AudioSink``) — synthesized PCM is pushed into
   the game's existing audio middleware, so an AI voice becomes an ordinary
   Wwise/FMOD voice and inherits attenuation, occlusion, busses, and reverb
   for free. No parallel audio path, no second mix to maintain.
"""

from voicert.game.budget import ComputeBudget, VoiceCostModel
from voicert.game.lod import DialogueTier, LODConfig, NPCState, DialogueLOD
from voicert.game.pool import NPCVoicePool, PooledVoice
from voicert.game.sinks import AudioSink, FMODProgrammerSink, NullSink, WwiseAudioInputSink

__all__ = [
    "AudioSink",
    "ComputeBudget",
    "DialogueLOD",
    "DialogueTier",
    "FMODProgrammerSink",
    "LODConfig",
    "NPCState",
    "NPCVoicePool",
    "NullSink",
    "PooledVoice",
    "VoiceCostModel",
    "WwiseAudioInputSink",
]
