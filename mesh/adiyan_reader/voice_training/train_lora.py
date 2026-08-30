"""LoRA fine-tune of Orpheus on Bharani's own voice, adapted from
canopyai/Orpheus-TTS's own finetune/lora.py (github.com/canopyai/
Orpheus-TTS/blob/main/finetune/lora.py) for local Apple Silicon (MPS)
instead of their CUDA + flash_attention_2 + wandb setup:

- attn_implementation left as PyTorch's default (sdpa) - flash_attention_2
  is CUDA-only, has no MPS build.
- device is "mps" instead of relying on Trainer's CUDA auto-detect.
- wandb dropped - report_to="none"; this is a single local run, not a
  tracked sweep.
- Reads the local JSONL encode_dataset.py produced instead of pulling a
  pushed-to-hub HF dataset.
- `modules_to_save=["lm_head", "embed_tokens"]` dropped from their
  reference LoraConfig. Confirmed live: with it, the frozen base loads
  fine (~4.3B params in fp32), but system free memory measured via
  vm_stat dropped to ~73MB at load time on this 16GB Mac - the optimizer
  state Adam needs for ~1B fully-trained params (lm_head+embed_tokens are
  large: ~156k-token vocab x 3072 hidden) would push training itself well
  past 16GB. Our new speaker name ("bharani") tokenizes into existing
  subword tokens already in vocabulary - it's not a new token id the way
  a genuinely unseen symbol would be - so the embedding table doesn't
  strictly need retraining for this case the way it might for a language
  needing new script/tokens. This is a disclosed deviation from Canopy
  Labs' own recipe made for local memory constraints, not a hidden one.
- Frozen base loaded in bfloat16 instead of float32 for the same memory
  reason - LoRA adapters and modules_to_save still train in fp32
  internally (peft's default), only the frozen weights are halved.

LoRA hyperparameters (rank, alpha, target_modules) are copied unchanged
from their reference script - no local retuning basis to justify
deviating from Canopy Labs' own defaults yet.

This is a genuine first attempt on hardware Canopy Labs never validated
for training (MPS) - it may hit an unsupported op or run out of memory
for this 3B model even under LoRA. If it does, the failure itself (not a
guess) determines whether a rented GPU is the next step.

Usage: python3 -m mesh.adiyan_reader.voice_training.train_lora
"""
import json
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

BASE_MODEL_DIR = Path(__file__).parent / 'models' / 'orpheus-3b-0.1-ft'
ENCODED_PATH = Path.home() / '.Adiyan' / 'voice_training' / 'bharani' / 'encoded_dataset.jsonl'
OUTPUT_DIR = Path.home() / '.Adiyan' / 'voice_training' / 'bharani' / 'checkpoints'

PAD_TOKEN = 128263  # matches canopyai/Orpheus-TTS's finetune/config.yaml

LORA_RANK = 32
LORA_ALPHA = 64
LORA_DROPOUT = 0.0


def main() -> None:
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    print(f'Using device: {device}')

    rows = []
    with open(ENCODED_PATH) as f:
        for line in f:
            row = json.loads(line)
            rows.append({
                'input_ids': row['input_ids'],
                'labels': row['labels'],
                'attention_mask': row['attention_mask'],
            })
    ds = Dataset.from_list(rows)
    print(f'Loaded {len(ds)} training examples')

    tokenizer = AutoTokenizer.from_pretrained(str(BASE_MODEL_DIR))
    model = AutoModelForCausalLM.from_pretrained(str(BASE_MODEL_DIR), dtype=torch.bfloat16)
    model.to(device)

    lora_config = LoraConfig(
        r=LORA_RANK,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'down_proj', 'up_proj'],
        bias='none',
        task_type='CAUSAL_LM',
        use_rslora=True,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    training_args = TrainingArguments(
        num_train_epochs=3,  # more epochs than their 1-epoch default - 69 examples is far below their recommended 300/speaker
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,
        logging_steps=1,
        output_dir=str(OUTPUT_DIR),
        report_to='none',
        save_steps=50,
        remove_unused_columns=True,
        learning_rate=5e-5,
    )

    trainer = Trainer(model=model, args=training_args, train_dataset=ds)
    trainer.train()

    merged_model = model.merge_and_unload()
    merged_model.save_pretrained(str(OUTPUT_DIR / 'merged'))
    tokenizer.save_pretrained(str(OUTPUT_DIR / 'merged'))
    print(f"Merged model saved to {OUTPUT_DIR / 'merged'}")


if __name__ == '__main__':
    main()
