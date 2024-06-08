import torch
import torch.nn as nn
from torch.nn import functional as F
import mmap
import random
import pickle
import argparse
import itertools

# Check if CUDA is available and if so, set the device accordingly
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(device)

# Define the parameters for the model and training
block_size = 128
batch_size = 64
max_iters = 3000
eval_interval = 500
learning_rate = 3e-4
eval_iters = 250
n_embd = 384
n_layer = 8
n_head = 8
dropout = 0.25

# Read characters from the vocabulary file
chars = ""
with open("training_data/vocab.txt", "r", encoding="utf-8") as f:
    text = f.read()
    chars = sorted(list(set(text)))

vocab_size = len(chars)
string_to_int = {ch: i for i, ch in enumerate(chars)}
int_to_string = {i: ch for i, ch in enumerate(chars)}
encode = lambda s: [string_to_int[c] for c in s]
decode = lambda l: ''.join([int_to_string[i] for i in l])


def get_random_chunk(split):
    filename = "training_data/train_split.txt" if split == 'train' else "training_data/val_split.txt"
    with open(filename, 'rb') as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            file_size = len(mm)
            start_pos = random.randint(0, file_size - block_size * batch_size)
            mm.seek(start_pos)
            block = mm.read(block_size * batch_size - 1)
            decoded_block = block.decode('utf-8', errors='ignore').replace('\r', '')
            data = torch.tensor(encode(decoded_block), dtype=torch.long)
    return data


def get_batch(split):
    data = get_random_chunk(split)
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([data[i:i + block_size] for i in ix])
    y = torch.stack([data[i + 1:i + block_size + 1] for i in ix])
    x, y = x.to(device), y.to(device)
    return x, y


class Head(nn.Module):
    def __init__(self, head_size):
        super().__init__()
        self.key = nn.Linear(n_embd, head_size, bias=False)
        self.query = nn.Linear(n_embd, head_size, bias=False)
        self.value = nn.Linear(n_embd, head_size, bias=False)
        self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size)))
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        B, T, C = x.shape
        k = self.key(x)
        q = self.query(x)
        wei = q @ k.transpose(-2, -1) * k.shape[-1] ** -0.5
        wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf'))
        wei = F.softmax(wei, dim=-1)
        wei = self.dropout(wei)
        v = self.value(x)
        out = wei @ v
        return out


class MultiHeadAttention(nn.Module):
    def __init__(self, num_heads, head_size):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size) for _ in range(num_heads)])
        self.proj = nn.Linear(head_size * num_heads, n_embd)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = torch.cat([h(x) for h in self.heads], dim=-1)
        out = self.dropout(self.proj(out))
        return out


class FeedFoward(nn.Module):
    def __init__(self, n_embd):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_embd, 4 * n_embd),
            nn.ReLU(),
            nn.Linear(4 * n_embd, n_embd),
            nn.Dropout(dropout),
        )

    def forward(self, x):
        return self.net(x)


class Block(nn.Module):
    def __init__(self, n_embd, n_head):
        super().__init__()
        head_size = n_embd // n_head
        self.sa = MultiHeadAttention(n_head, head_size)
        self.ffwd = FeedFoward(n_embd)
        self.ln1 = nn.LayerNorm(n_embd)
        self.ln2 = nn.LayerNorm(n_embd)

    def forward(self, x):
        y = self.sa(x)
        x = self.ln1(x + y)
        y = self.ffwd(x)
        x = self.ln2(x + y)
        return x

class GPTLanguageModel(nn.Module):
    def __init__(self, vocab_size):
        super().__init__()
        self.token_embedding_table = nn.Embedding(vocab_size, n_embd)
        self.position_embedding_table = nn.Embedding(block_size, n_embd)
        self.blocks = nn.Sequential(*[Block(n_embd, n_head=n_head) for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(n_embd)
        self.lm_head = nn.Linear(n_embd, vocab_size)
        self.apply(self._init_weights)

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, index, targets=None):
        B, T = index.shape
        tok_emb = self.token_embedding_table(index)
        pos_emb = self.position_embedding_table(torch.arange(T, device=device))
        x = tok_emb + pos_emb
        x = self.blocks(x)
        x = self.ln_f(x)
        logits = self.lm_head(x)
        if targets is None:
            loss = None
        else:
            B, T, C = logits.shape
            logits = logits.view(B * T, C)
            targets = targets.view(B * T)
            loss = F.cross_entropy(logits, targets)
        return logits, loss

    def generate(self, index, max_new_tokens):
        for _ in range(max_new_tokens):
            index_cond = index[:, -block_size:]
            logits, loss = self.forward(index_cond)
            logits = logits[:, -1, :]
            probs = F.softmax(logits, dim=-1)
            index_next = torch.multinomial(probs, num_samples=1)
            index = torch.cat((index, index_next), dim=1)
        return index


@torch.no_grad()
def estimate_loss(model):
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

# Hyperparameter search
def hyperparameter_search():
    # Define the search space including n_embd, n_layer, and n_head
    learning_rates = [1e-5, 1e-4, 3e-4, 1e-3, 5e-3]
    batch_sizes = [16, 32, 64, 128, 256]
    dropouts = [0.1, .15, 0.25, .4, 0.5]
    n_embds = [128, 192, 256, 320, 384, 448, 512]
    n_layers = [6, 8, 10]
    n_heads = [4, 8, 12]

    best_val_loss = float('inf')
    best_hyperparams = None

    for lr, bs, dr, embd, layer, head in itertools.product(learning_rates, batch_sizes, dropouts, n_embds, n_layers, n_heads):
        print(f"Evaluating combination: lr={lr}, batch_size={bs}, dropout={dr}, n_embd={embd}, n_layer={layer}, n_head={head}")

        global batch_size, dropout, n_embd, n_layer, n_head
        batch_size = bs
        dropout = dr
        n_embd = embd
        n_layer = layer
        n_head = head

        # Check VRAM usage
        vram_usage = torch.cuda.memory_allocated()
        if vram_usage > 9.5e9:  # 9.5GB in bytes
            print("Skipping current iteration due to excessive VRAM usage")
            continue

        model = GPTLanguageModel(vocab_size).to(device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max_iters)
        scaler = torch.cuda.amp.GradScaler()

        for iter in range(max_iters):
            if iter % eval_interval == 0:
                losses = estimate_loss(model)
                val_loss = losses['val']
                print(f"step: {iter}, train loss: {losses['train']:.3f}, val loss: {val_loss:.3f}")

                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    best_hyperparams = (lr, bs, dr, embd, layer, head)
                    torch.save(model.state_dict(), "best_model.pt")

            xb, yb = get_batch('train')

            with torch.cuda.amp.autocast():
                logits, loss = model.forward(xb, yb)

            optimizer.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()

        print(f"Finished combination: lr={lr}, batch_size={bs}, dropout={dr}, n_embd={embd}, n_layer={layer}, n_head={head}, val_loss={val_loss:.3f}")

    print(f"Best hyperparameters found: lr={best_hyperparams[0]}, batch_size={best_hyperparams[1]}, dropout={best_hyperparams[2]}, n_embd={best_hyperparams[3]}, n_layer={best_hyperparams[4]}, n_head={best_hyperparams[5]} with val_loss={best_val_loss:.3f}")



# Run the hyperparameter search
hyperparameter_search()

# Load the best model and generate text
model = GPTLanguageModel(vocab_size)
model.load_state_dict(torch.load("best_model.pt"))
model = model.to(device)

prompt = 'Hello! Can you see me?'
context = torch.tensor(encode(prompt), dtype=torch.long, device=device)
generated_chars = decode(model.generate(context.unsqueeze(0), max_new_tokens=100)[0].tolist())
print(generated_chars)
