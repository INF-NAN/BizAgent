"""LoRA SFT of the agent model on teacher conversations (runs in the GPU venv).

Each sample is rendered with the model's own chat template and the same tools, exactly as the
serving engine renders a request. Loss is on assistant turns only: the tokens after
``<|im_start|>assistant\\n`` up to and including ``<|im_end|>``.

For a template without thinking (Qwen3-*-Instruct-2507, the default model) every request the
model is served is an exact prefix of the rendered conversation, so each turn is learnt after
exactly the text it follows when served. A hybrid template (Qwen3-4B with thinking off) starts
every generation after an empty ``<think>\\n\\n</think>\\n\\n`` block but writes it only for the
last turn; it is then inserted (without loss) before every earlier assistant turn. Before
training, the script renders the served prompt of every assistant turn of the first samples and
counts the turns whose prompt is not a prefix of the training text (``template_check`` in
train_meta.json): 0 for the default model.
Logits are computed only where a loss is taken (``logits_to_keep`` with indices).
Adapters are saved every ``--save-every`` epochs (fractions allowed) under ``<out>/ckpt-<k>``.

usage: python scripts/lab/sft_train.py --data data/lab/sft/train.jsonl --model <dir> --out <dir>
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import random
import time
from pathlib import Path
from typing import Any

import torch
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

ASSISTANT = "<|im_start|>assistant\n"
EMPTY_THINK = "<think>\n\n</think>\n\n"
END = "<|im_end|>"


def prepare(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = dict(c["function"])
                if isinstance(fn.get("arguments"), str):
                    with contextlib.suppress(json.JSONDecodeError):
                        fn["arguments"] = json.loads(fn["arguments"])
                calls.append({**c, "function": fn})
            m["tool_calls"] = calls
        if m.get("content") is None:
            m["content"] = ""
        out.append(m)
    return out


def thinking_prefix(tok: Any) -> bool:
    """True if served generations start after an empty think block (hybrid templates)."""
    prompt = tok.apply_chat_template(
        [{"role": "user", "content": "x"}], tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    return bool(prompt.endswith(EMPTY_THINK))


def render(tok: Any, sample: dict[str, Any], think: bool) -> tuple[str, list[tuple[int, int]]]:
    """Template text (with an empty think block before every assistant turn if ``think``) and the
    character spans that are trained."""
    text = tok.apply_chat_template(
        prepare(sample["messages"]),
        tools=sample.get("tools") or None,
        tokenize=False,
        add_generation_prompt=False,
        enable_thinking=False,
    )
    parts = text.split(ASSISTANT)
    rebuilt = parts[0]
    spans = []
    for part in parts[1:]:
        rebuilt += ASSISTANT
        if think and not part.startswith("<think>"):
            part = EMPTY_THINK + part
        skip = part.index("</think>") + len("</think>\n\n") if part.startswith("<think>") else 0
        body_start = len(rebuilt) + skip
        end = part.find(END)
        rebuilt += part
        if end >= 0:
            spans.append((body_start, len(rebuilt) - len(part) + end + len(END)))
    return rebuilt, spans


def template_check(tok: Any, samples: list[dict[str, Any]], think: bool) -> dict[str, int]:
    """Assistant turns whose served prompt is / is not a prefix of the training text."""
    ok = bad = 0
    for sample in samples:
        text, _ = render(tok, sample, think)
        msgs = prepare(sample["messages"])
        for i, m in enumerate(msgs):
            if m["role"] != "assistant":
                continue
            served = tok.apply_chat_template(
                msgs[:i],
                tools=sample.get("tools") or None,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            if text.startswith(served):
                ok += 1
            else:
                bad += 1
    return {"turns_checked": ok + bad, "prompt_not_prefix": bad}


def encode(tok: Any, sample: dict[str, Any], max_len: int, think: bool) -> dict[str, Any] | None:
    text, spans = render(tok, sample, think)
    enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    ids = enc["input_ids"]
    if len(ids) > max_len:
        return None
    labels = [-100] * len(ids)
    for i, (s, e) in enumerate(enc["offset_mapping"]):
        if any(a <= s and e <= b and e > s for a, b in spans):
            labels[i] = ids[i]
    if all(x == -100 for x in labels):
        return None
    return {"input_ids": ids, "labels": labels}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--save-every", type=float, default=0.5)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--warmup", type=float, default=0.03)
    ap.add_argument("--batch", type=int, default=16, help="sequences per optimizer step")
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--rank", type=int, default=64)
    ap.add_argument("--alpha", type=int, default=128)
    ap.add_argument("--dropout", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(args.model)
    samples = [
        json.loads(line) for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    think = thinking_prefix(tok)
    check = template_check(tok, samples[:20], think)
    print(f"template: empty think block inserted={think}; {check}", flush=True)
    data, too_long = [], 0
    for s in samples:
        enc = encode(tok, s, args.max_len, think)
        if enc is None:
            too_long += 1
        else:
            data.append(enc)
    trained_tokens = sum(sum(1 for x in d["labels"] if x != -100) for d in data)
    total_tokens = sum(len(d["input_ids"]) for d in data)
    print(
        f"samples={len(data)} dropped={too_long} tokens={total_tokens} trained_tokens={trained_tokens}",
        flush=True,
    )

    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16, attn_implementation="sdpa")
    model.to(args.device)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model.config.use_cache = False
    lora = LoraConfig(
        r=args.rank,
        lora_alpha=args.alpha,
        lora_dropout=args.dropout,
        target_modules="all-linear",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    steps_per_epoch = math.ceil(len(data) / args.batch)
    total_steps = max(1, round(steps_per_epoch * args.epochs))
    save_every = max(1, round(steps_per_epoch * args.save_every))
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0, betas=(0.9, 0.999))
    sched = get_cosine_schedule_with_warmup(opt, max(1, round(args.warmup * total_steps)), total_steps)

    order: list[int] = []
    log = (args.out / "train_log.jsonl").open("a", encoding="utf-8")
    started = time.time()
    model.train()
    for step in range(1, total_steps + 1):
        batch = []
        while len(batch) < args.batch:
            if not order:
                order = list(range(len(data)))
                random.shuffle(order)
            batch.append(data[order.pop()])
        n_tok = sum(sum(1 for x in d["labels"] if x != -100) for d in batch)
        loss_sum = 0.0
        for d in batch:
            ids = torch.tensor([d["input_ids"]], device=args.device)
            labels = torch.tensor(d["labels"], device=args.device)
            # position p predicts token p + 1: keep logits where the next token is trained
            keep = (labels[1:] != -100).nonzero(as_tuple=True)[0]
            out = model(input_ids=ids, logits_to_keep=keep)
            logits = out.logits[0].float()
            loss = torch.nn.functional.cross_entropy(logits, labels[keep + 1], reduction="sum") / n_tok
            loss.backward()
            loss_sum += float(loss.detach())
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
        sched.step()
        opt.zero_grad(set_to_none=True)
        rec = {
            "step": step,
            "epoch": round(step / steps_per_epoch, 3),
            "loss": round(loss_sum, 5),
            "lr": sched.get_last_lr()[0],
            "elapsed_s": round(time.time() - started, 1),
        }
        log.write(json.dumps(rec) + "\n")
        log.flush()
        if step % 10 == 0 or step == 1:
            print(rec, flush=True)
        if step % save_every == 0 or step == total_steps:
            name = f"ckpt-{step}"
            model.save_pretrained(args.out / name)
            print(f"saved {name} at epoch {rec['epoch']}", flush=True)
    meta = {
        "samples": len(data),
        "dropped_too_long": too_long,
        "think_block_inserted": think,
        "template_check": check,
        "tokens": total_tokens,
        "trained_tokens": trained_tokens,
        "steps": total_steps,
        "steps_per_epoch": steps_per_epoch,
        "args": {k: str(v) for k, v in vars(args).items()},
        "train_s": round(time.time() - started, 1),
    }
    (args.out / "train_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
