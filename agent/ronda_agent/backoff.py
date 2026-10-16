import random


class Backoff:
    """Exponential backoff 5 → 10 → 20 … maks 300 detik, jitter ±20%."""

    def __init__(self, base: float = 5.0, maximum: float = 300.0, jitter: float = 0.2, rng: random.Random | None = None):
        self.base, self.maximum, self.jitter = base, maximum, jitter
        self.rng = rng or random.Random()
        self.failures = 0

    def next_delay(self) -> float:
        delay = min(self.maximum, self.base * 2 ** self.failures)
        self.failures += 1
        return delay * self.rng.uniform(1 - self.jitter, 1 + self.jitter)

    def reset(self) -> None:
        self.failures = 0
