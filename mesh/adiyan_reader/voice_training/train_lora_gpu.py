"""LoRA fine-tune of Orpheus on Bharani's own voice - GPU variant.

This is the CUDA counterpart of train_lora.py (which is what actually ran,
and swap-thrashed twice, on the local M2 Pro Mac - see that file's own
docstring for the exact memory numbers that ruled out local training).
This version restores what Canopy Labs' own reference script
(github.com/canopyai/Orpheus-TTS/blob/main/finetune/lora.py) does that
the Mac couldn't afford:

- flash_attention_2 - CUDA-only, meaningfully faster and lower-memory
  than PyTorch's default attention on real GPU hardware.
- modules_to_save=["lm_head", "embed_tokens"] restored - dropped locally
  purely for memory, back in now that a real GPU has headroom for it.
- bf16 mixed-precision training (not just bf16-loaded frozen weights).
- wandb kept optional (off by default here too - a single run doesn't
  need a tracked sweep, but this is easy to flip back on if wanted).

Same LoRA hyperparameters (rank 32, alpha 64) and 3-epoch count as the
Mac version - unchanged because the reasoning behind them (69 examples
being below Canopy Labs' recommended 300/speaker) doesn't depend on
which hardware runs the job.

Usage on a rented GPU box (see README.md in this directory for where to
rent one and how to get here):
  pip install -r requirements-gpu.txt
  huggingface-cli login   # same gated-repo access used to download the base model locally
  python3 train_lora_gpu.py
"""
import json
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

BASE_MODEL_NAME = 'canopylabs/orpheus-3b-0.1-ft'  # pulled fresh from HF on the GPU box, not copied from the Mac
ENCODED_PATH = Path(__file__).parent / 'encoded_dataset.jsonl'  # copy this file over from the Mac (~1MB)
OUTPUT_DIR = Path(__file__).parent / 'checkpoints'

LORA_RANK = 32
LORA_ALPHA = 64
LORA_DROPOUT = 0.0


def main() -> None:
    if not torch.cuda.is_available():
        raise RuntimeError(
            'No CUDA GPU visible - this script is the rented-GPU variant. '
            'Use train_lora.py for local/MPS (already confirmed to swap-thrash on 16GB Macs).'
        )

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

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_NAME)
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL_NAME, dtype=torch.bfloat16, attn_implementation='flash_attention_2',
    )

    lora_config = LoraConfig(
        r=LORA_RANK,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'down_proj', 'up_proj'],
        bias='none',
        modules_to_save=['lm_head', 'embed_tokens'],
        task_type='CAUSAL_LM',
        use_rslora=True,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    training_args = TrainingArguments(
        num_train_epochs=3,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,
        logging_steps=1,
        bf16=True,
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
