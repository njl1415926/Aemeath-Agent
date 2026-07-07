# Audio / TTS Module Status

## Summary

`Aemeath_roleplay_skill/audio/` is the local Audio workspace for Aemeath GPT-SoVITS
experiments. It currently contains a trained GPT-SoVITS v2ProPlus local workflow,
including annotation tooling, training scripts, inference console, local runtime,
model weights, training artifacts, source audio, and generated outputs.

This module is intentionally not connected to the Aemeath Agent runtime yet.

## Current Decision

Audio is kept as a standalone experimental workspace.

It does not currently affect:

- `DialogueController`
- Character
- RAG / Evidence
- Memory
- Prompt assembly
- Session
- CLI output
- future API output

The main reason is deployment cost and operational complexity. GPT-SoVITS inference
is GPU-oriented, and keeping it always loaded on a server may be expensive. The
text Agent should remain deployable without a GPU.

## Local Workspace

Main path:

```text
Aemeath_roleplay_skill/audio/
```

Important local files:

- `README.md`
- `AUDIO_STATUS_FOR_MANAGER.md`
- `aemeath_tts_console.py`
- `train_aemeath_v2proplus.py`
- `run_zero_shot_ab.py`
- `segment_annotator/`

Local heavy artifacts:

- `archives/`
- `assets/`
- `models/`
- `outputs/`
- `training/`
- `vendor/`

The whole `Aemeath_roleplay_skill/audio/` workspace is ignored by git for now.
The normal source repository records only this architecture note until Audio is
ready to become a real adapter or service.

## Current Local Capability

The audio workspace has a local GPT-SoVITS v2ProPlus path:

- voice data整理
- segment annotation
- GPT-SoVITS training list export
- v2ProPlus fine-tuning
- persistent TTS console
- local WAV generation

The current implementation is still local and operator-driven, not a service API.

## Future Runtime Boundary

When Audio is ready to enter the product flow, it should be added behind a port
such as `TextToSpeechPort` or `AudioOutputPort`.

Recommended flow:

```text
AgentTurnOutput.response
  -> optional Audio/TTS adapter
  -> audio file / URL / stream metadata
```

Audio must remain optional:

- Text reply returns first.
- TTS failure does not fail the text turn.
- GPT-SoVITS details stay inside an adapter or external service.
- CLI / Web / API can decide whether to request audio.

## Deployment Notes

For server deployment, prefer one of these modes:

1. Text-only deployment by default.
2. Optional separate TTS service on a GPU machine.
3. Optional remote/cloud TTS adapter.
4. Local GPT-SoVITS adapter only for development or dedicated GPU hosts.

Do not load GPT-SoVITS weights inside the main web API process unless the deployment
is intentionally GPU-backed and resource-isolated.

## Follow-up Tasks

- Define `TextToSpeechPort` / `AudioOutputPort`.
- Decide whether Audio is called by API/Web adapter or an application service.
- Add trace/app-log fields for TTS provider, latency, output path, and failure reason.
- Add config flags to enable or disable Audio at runtime.
- Design reference-audio or emotion selection without writing emotion tags into spoken text.
- Evaluate whether GPT-SoVITS should run as a persistent sidecar service.
