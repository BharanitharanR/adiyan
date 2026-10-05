# Local patches to third-party components

`voicebox/` is a clone of https://github.com/jamiepine/voicebox, ignored by this repo. Changes made to it for
Adiyan live here so they're versioned with Adiyan and can be re-applied after updating Voicebox.

| Patch | Why |
| --- | --- |
| `voicebox-long-transcription.patch` | `/transcribe` returned only the first ~30 s of longer recordings (Whisper's processor truncates by default). Needed by AdiyanReader's audio-quality eval and the genie's `listen_and_check`. |

Re-apply after pulling a new Voicebox:

```bash
cd voicebox && git am ../patches/voicebox-long-transcription.patch
```

Then restart Voicebox by stopping its process and starting it again (`mesh/start_all.sh restart voicebox` doesn't
currently stop it: its match pattern includes the `env PYTHONPATH=...` prefix, which isn't in the running command).
