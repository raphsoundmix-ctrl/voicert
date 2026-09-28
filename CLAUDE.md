# VoiceRT — repository instructions

## Positioning (do not drift)

> We help Unity game developers escape the cost, time, and localization limits of pre-recorded voice-over by shipping one FMOD-native, interruptible AI voice agent, so players can talk to any NPC and hear it answer live, in character, inside the game's own mix.

- Two surfaces, two jobs. The site (`docs/index.html`) is for investors, accelerators and studio leads: the problem, the proof, the market, the model, the ask. The README and `docs/*.md` are for engineers: architecture, protocol, measurements, limits. Technical depth moves to the repo, never the other way.
- Public surfaces (README, `docs/index.html`, GitHub About and topics, `pyproject.toml` description) mention **one product**: an AI voice agent for game NPCs.
- Bridge order, everywhere: **Unity + FMOD → Unity plain `AudioSource` → Unreal + Wwise**. Write "FMOD or Wwise" and "Unity or Unreal", never the reverse order.
- The NPC profile is the only profile. The old `sales` and `assistant` profiles and the telephony / WebRTC transport skeletons were deleted on 2026-09-28; do not reintroduce another product's prompt, tools or transport.
- Banned words: seamless, revolutionary, leverage, unlock. No emoji in copy. Banned topics: telephony (SIP, Twilio, μ-law, 8 kHz), CRM, personal assistant, IoT, calendar, business owner.
- Honesty: keep every caveat (Unreal + Wwise glue not exercised inside an Editor, not benchmarked on a phone, English-only speech recognition, working prototype). Never upgrade a claim. Traction is what exists: no invented users, revenue or round.
- Competitor claims carry a date and a source. Do not claim to be the only local voice pipeline: NVIDIA ACE ships free on-device plugins for Unreal (2026). The difference is Unity + FMOD.
- The test count comes from `pytest -q` (and `dotnet test` for C#). Never hardcode a different number.

Full document: `docs/POSITIONING.md`.

## Before saying "done"

```bash
pytest -q && mypy
grep -rniE "jarvis|twilio|\bsip\b|\bcrm\b|\biot\b|calendar|business owner|μ-law|u-law|telephony|one core, many jobs|three (profiles|modes)|Local and private|\(51\)" README.md docs/index.html docs/*.md   # expect 0
grep -rniE "wwise[^.]{0,30}fmod|unreal[^.]{0,30}unity" README.md docs/index.html docs/*.md              # expect 0
python -c "import re;h=open('docs/index.html',encoding='utf-8').read();ids=set(re.findall(r'id=\"([^\"]+)\"',h));print('dead anchors:',[a for a in re.findall(r'href=\"#([^\"]+)\"',h) if a not in ids])"
```

Allowlist for the greps: `state.interrupt_assistant` (code identifier) and the one-line design-reference credit at the bottom of the README.
