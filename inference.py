from llama.tokenizer import Tokenizer
from llama.model import ModelArgs, Llama
import torch


def inference():
    torch.manual_seed(1)

    tokenizer_path = "/home1/ichuncha/llama/llama2-7b/tokenizer.model"
    model_path = "/home1/ichuncha/llama/llama2-7b/consolidated.00.pth"
    lora_weights_path = "/home1/ichuncha/llama/weights/lora_weights_32.pth"

    tokenizer = Tokenizer(tokenizer_path)

    checkpoint = torch.load(model_path, map_location="cpu")
    lora_state_dict = torch.load(lora_weights_path, map_location="cpu")

    model_args = ModelArgs()
    torch.set_default_tensor_type(torch.cuda.HalfTensor) # load model in fp16

    model = Llama(model_args)
    model.load_state_dict(checkpoint, strict=False)
    # model.load_state_dict(lora_state_dict, strict=False)
    model.to("cuda")
    
    prompts = [
        # For these prompts, the expected answer is the natural continuation of the prompt
        "Describe the structure of an atom.",
        "Make the following sentence more concise: I have a really bad cold and it is making me feeling really miserable.",
        "I believe the meaning of life is",
        "Simply put, the theory of relativity states that ",
        """A brief message congratulating the team on the launch:

        Hi everyone,
        
        I just """,
        # Few shot prompt (providing a few examples before asking model to complete more);
        """Translate English to French:
        
        sea otter => loutre de mer
        peppermint => menthe poivrée
        plush girafe => girafe peluche
        cheese =>""",
    ]

    model.eval()
    results = model.generate(tokenizer, prompts, max_gen_len=64, temperature=0.6, top_p=0.9)

    for prompt, result in zip(prompts, results):
        print(prompt)
        print(f"> {result['generation']}")
        print("\n==================================\n")

    
if __name__ == "__main__":
    inference()