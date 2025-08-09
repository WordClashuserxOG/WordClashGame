import random

def make_partial(word: str, reveal_count: int):
    letters = list(word)
    hidden = ['_' if c.isalpha() else c for c in letters]
    revealed_indices = random.sample(range(len(word)), min(reveal_count, len(word)))
    for idx in revealed_indices:
        hidden[idx] = word[idx]
    return ' '.join(hidden)
