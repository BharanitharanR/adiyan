# Bharani's own-voice fine-tune of Orpheus

## What's already done (this Mac)

- `prepare_dataset.py` converted 69 recorded clips (of 71 sent - see its
  own output for the 2 gaps) into 24kHz mono WAV + `manifest.jsonl`.
- `encode_dataset.py` ran every clip through SNAC and built
  `encoded_dataset.jsonl` (~1MB) - the actual training-ready format,
  matching Canopy Labs' own data-prep notebook token-for-token.
- `train_lora.py` (the MPS/local variant) was tried twice, once with the
  full mesh running and once with it stopped. Both times it did real
  work briefly, then swap-thrashed once the backward pass/optimizer step
  needed more memory than the Mac has (confirmed via `sysctl
  vm.swapusage` climbing to its ceiling both times, not a guess). This
  isn't a bug - a 3B-parameter LoRA fine-tune genuinely doesn't fit in
  16GB unified memory with room to actually train.
- `train_lora_gpu.py` is the CUDA-ready version, restoring what the Mac
  version had to drop for memory (flash_attention_2, full bf16 training,
  `modules_to_save` for the embedding/lm_head).

## What you need to do: rent a GPU

Only `encoded_dataset.jsonl` (~1MB) needs to travel - the base model
downloads fresh from Hugging Face on whatever box you rent, same as it
did here.

### Where to rent one

| Provider | Why | Rough cost |
|---|---|---|
| **RunPod** (runpod.io) | Easiest for a one-off job - web UI, pick a GPU, get SSH access in under a minute, billed per-second while running, stop it and stop paying. Pre-built PyTorch templates. | RTX 4090 ~$0.35-0.70/hr, RTX 3090 ~$0.25-0.45/hr |
| **Vast.ai** (vast.ai) | Cheapest, because it's a marketplace of other people's GPUs - more variability in reliability/setup, more manual. | Often 20-40% cheaper than RunPod for the same card |
| **Lambda Labs** (lambdalabs.com) | Reliable, simple, but on-demand GPUs are sometimes waitlisted/sold out. | Similar to RunPod |

For a single short fine-tune like this, **RunPod is the easiest starting
point** - a 3090 or 4090 pod with a "PyTorch" template already has
CUDA + torch ready, and this whole job (69 examples, 3 epochs) should
finish in minutes, not hours, so total cost is well under $1.

### Steps once you have a pod/instance with a GPU and SSH access

1. SSH in, then copy this directory's `encoded_dataset.jsonl`,
   `train_lora_gpu.py`, and `requirements-gpu.txt` over (`scp`, or paste
   directly - it's one ~1MB file plus two small scripts).
2. `pip install -r requirements-gpu.txt`
3. `huggingface-cli login` - use the same access-token flow you already
   did for the gated `canopylabs/orpheus-3b-0.1-ft` repo (your account
   needs continued access; the token itself doesn't need to be the exact
   same one, just from an account with access).
4. `python3 train_lora_gpu.py`
5. Once it finishes, the merged model is in `checkpoints/merged/` on that
   box - copy that back here (or straight into wherever the mesh's
   Orpheus serving path expects a custom voice model; that wiring
   - adding "bharani" as a selectable voice in `tts.py`/`config_sdk` -
    is a separate step after training actually succeeds).

## Restarting the mesh

Stopped earlier to free memory for the (unsuccessful) local attempt:

```bash
bash mesh/start_all.sh
```
