# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (`main`) | yes |

This is a prototype with one development line. Fixes land on `main`; there are no maintained older branches.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"), or email raphsoundmix@gmail.com. Expect an acknowledgement within 7 days. Please do not open a public issue for anything exploitable.

## Scope notes

- The engine bridge (`voicert.game.bridge`) is a plain TCP server meant for `127.0.0.1` or a trusted LAN. It has no authentication and no encryption. Do not expose it on a public interface.
- The NPC tool set is closed by construction: tools outside the game engine are not in the registry, and a prompt cannot add them. That is a guardrail against prompt injection reaching the game, not a sandbox around the LLM provider.
- Provider adapters read API keys from the environment. Never commit them.
