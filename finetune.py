import copy
import json
import logging
import os

import torch
from torch.utils.data import Dataset
from dataclasses import dataclass

from llama.tokenizer import Tokenizer
from llama.model import ModelArgs, Llama
from torch.cuda.amp import autocast, GradScaler

IGNORE_INDEX = -100

PROMPT_DICT = {
    "prompt_input": (
        "Below is an instruction that describes a task, paired with an input that provides further context. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:"
    ),
    "prompt_no_input": (
        "Below is an instruction that describes a task. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Response:"
    ),
}


def _tokenize_fn(strings, tokenizer):
    """Tokenize a list of strings."""
    tokenized_list = [
        torch.tensor(tokenizer.encode(text, bos=True, eos=True)) for text in strings
    ]
    input_ids = labels = [tokenized for tokenized in tokenized_list]
    input_ids_lens = labels_lens = [
        tokenized.ne(-1).sum().item() for tokenized in tokenized_list
    ]
    return dict(
        input_ids=input_ids,
        labels=labels,
        input_ids_lens=input_ids_lens,
        labels_lens=labels_lens,
    )


def preprocess(sources, targets, tokenizer):
    """Preprocess the data by tokenizing."""
    examples = [s + t for s, t in zip(sources, targets)]
    examples_tokenized, sources_tokenized = [_tokenize_fn(strings, tokenizer) for strings in (examples, sources)]
    input_ids = examples_tokenized["input_ids"]
    labels = copy.deepcopy(input_ids)
    for label, source_len in zip(labels, sources_tokenized["input_ids_lens"]):
        label[:source_len-1] = IGNORE_INDEX
    return dict(input_ids=input_ids, labels=labels)


class SupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(self, data_path, tokenizer):
        super(SupervisedDataset, self).__init__()
        logging.warning("Loading data...")
        with open(data_path, "r") as f:
            list_data_dict = json.load(f)

        logging.warning("Formatting inputs...")
        prompt_input, prompt_no_input = PROMPT_DICT["prompt_input"], PROMPT_DICT["prompt_no_input"]
        sources = [
            prompt_input.format_map(example) if example.get("input", "") != "" else prompt_no_input.format_map(example)
            for example in list_data_dict
        ]
        targets = [f"{example['output']}" for example in list_data_dict]

        logging.warning("Tokenizing inputs... This may take some time...")
        data_dict = preprocess(sources, targets, tokenizer)

        self.input_ids = data_dict["input_ids"]
        self.labels = data_dict["labels"]

    def __len__(self):
        return len(self.input_ids)

    def __getitem__(self, i):
        return dict(input_ids=self.input_ids[i], labels=self.labels[i])


@dataclass
class DataCollatorForSupervisedDataset(object):
    """Collate examples for supervised fine-tuning."""
    def __call__(self, instances):
        input_ids, labels = tuple([instance[key] for instance in instances] for key in ("input_ids", "labels"))
        input_ids = torch.nn.utils.rnn.pad_sequence(
            input_ids, batch_first=True, padding_value=-1
        )
        labels = torch.nn.utils.rnn.pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)
        return dict(
            input_ids=input_ids,
            labels=labels,
            attention_mask=input_ids.ne(-1),
        )


def make_supervised_data_module(tokenizer, data_path):
    """Make dataset and collator for supervised fine-tuning."""
    train_dataset = SupervisedDataset(tokenizer=tokenizer, data_path=data_path)
    data_collator = DataCollatorForSupervisedDataset()
    return dict(train_dataset=train_dataset, eval_dataset=None, data_collator=data_collator)


def train():

    torch.manual_seed(1)

    model_path = "/home1/ichuncha/llama/llama2-7b/consolidated.00.pth"
    tokenizer_path = "/home1/ichuncha/llama/llama2-7b/tokenizer.model"
    data_path = "/home1/ichuncha/llama/alpaca_data_200.json"

    # load model
    checkpoint = torch.load(model_path, map_location="cpu")
    model_args = ModelArgs()
    model_args.n_layers = 32  # for debugging purposes we only use 1 layer
    # torch.set_default_tensor_type(torch.cuda.HalfTensor) # for training we use fp32 weights
    model = Llama(model_args)
    model.load_state_dict(checkpoint, strict=False)
    model.to("cuda")

    # load tokenizer
    tokenizer = Tokenizer(tokenizer_path)

    # create dataloader
    data_module = make_supervised_data_module(tokenizer=tokenizer, data_path=data_path)
    dataloader = torch.utils.data.DataLoader(
        data_module["train_dataset"],
        batch_size=1,
        collate_fn=data_module["data_collator"],
        shuffle=True,
    )

    for name, param in model.named_parameters():
        # print(name, param.requires_grad)
        # freeze model parameters except lora_a and lora_b 
        if "lora_" not in name: 
            param.requires_grad = False
    
    total_params = sum(p.numel() for p in model.parameters())
    trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Trainable params: {trainable_params}, percentage: {trainable_params / total_params * 100:.4f}%")

    # prepare optimizer and loss function
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
    criterion = torch.nn.CrossEntropyLoss(ignore_index=-100)

    # number of steps to accumulate gradients
    accumulation_steps = 8
    optimizer.zero_grad()

    model.train()

    # Gradient scaling to prevent underflow
    scaler = GradScaler()

    for epoch in range(10):
        for i, batch in enumerate(dataloader):
            input_ids = batch['input_ids'].to("cuda")
            labels = batch['labels'].to("cuda")

            # Runs forward pass with autocasting
            with autocast(dtype=torch.float16):
                logits = model(input_ids)

                shift_logits = logits[..., :-1, :].contiguous()
                shift_labels = labels[..., 1:].contiguous()
                shift_logits = shift_logits.view(-1, 32000)
                shift_labels = shift_labels.view(-1)

                loss = criterion(shift_logits, shift_labels) / accumulation_steps
            
            # Accumulate scaled gradients
            scaler.scale(loss).backward()

            # Simulate a larger batch size (to accumulation_steps)
            if (i + 1) % accumulation_steps == 0 or (i + 1) == len(dataloader):
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()

            if i % 50 == 0:
                # scale loss back for reporting
                print(f"epoch: {epoch:<5} step: {i:<5} loss: {loss.item() * accumulation_steps}")
    
    
    weights_save_path = "./weights/"
    os.makedirs(weights_save_path, exist_ok=True)

    # Save model parameters
    params_file_path = os.path.join(weights_save_path, "params.json")

    desired_keys = ["dim", "multiple_of", "n_heads", "n_layers",]
    model_params = {key: getattr(model.params, key, None) for key in desired_keys}
    with open(params_file_path, 'w') as params_file:
        json.dump(model_params, params_file)

    # Save model state
    model_state_path = os.path.join(weights_save_path, "lora_weights_32.pth")
    # Only store lora layer states as we freeze the rest during training
    lora_state_dict = {k: model.state_dict()[k] for k in model.state_dict() if 'lora_' in k}
    torch.save(lora_state_dict, model_state_path)
    print(f"Fine-tuned lora weights saved to {model_state_path}")


if __name__ == "__main__":
    train()