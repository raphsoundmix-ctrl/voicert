# VoiceRT — repository instructions

## Positioning (do not drift)

> We help Unity game developers give NPCs real, interruptible voice conversations without a recording budget, through one FMOD-native character asset, so players can talk to any NPC and get an in-character answer inside the game's own mix.

- Public surfaces (README, `docs/index.html`, GitHub About and topics, `pyproject.toml` description) mention **one product**: an AI voice agent for game NPCs.
- Bridge order, everywhere: **Unity + FMOD → Unity plain `AudioSource` → Unreal + Wwise**. Write "FMOD or Wwise" and "Unity or Unreal", never the reverse order.
- The `sales` and `assistant` profiles stay in `src/` and `tests/` and never appear on a public surface. One permitted line lives in `docs/architecture.md`: they exist in `ConfigFactory` only to prove tool sets cannot overlap.
- Banned words: seamless, revolutionary, leverage, unlock. No emoji in copy. Banned topics: telephony (SIP, Twilio, μ-law, 8 kHz), CRM, personal assistant, IoT, calendar, business owner.
- Honesty: keep every caveat (un-compiled inside an Editor, not benchmarked on a phone, working prototype). Never upgrade a claim. The FMOD sink is milestone M1 until it is compiled and heard.
- The test count comes from `pytest -q`. Never hardcode a different number.

Full document: `docs/POSITIONING.md`.

## Before saying "done"

```bash
pytest -q && mypy
grep -rniE "jarvis|twilio|\bsip\b|\bcrm\b|\biot\b|calendar|business owner|μ-law|u-law|telephony|one core, many jobs|three (profiles|modes)|Local and private|\(51\)" README.md docs/index.html docs/*.md   # expect 0
grep -rniE "wwise[^.]{0,30}fmod|unreal[^.]{0,30}unity" README.md docs/index.html docs/*.md              # expect 0
python -c "import re;h=open('docs/index.html',encoding='utf-8').read();ids=set(re.findall(r'id=\"([^\"]+)\"',h));print('dead anchors:',[a for a in re.findall(r'href=\"#([^\"]+)\"',h) if a not in ids])"
```

Allowlist for the greps: `state.interrupt_assistant` (code identifier) and the one-line design-reference credit at the bottom of the README.
