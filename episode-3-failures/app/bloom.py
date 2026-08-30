"""A Bloom filter, in about forty lines and no dependencies.

The point of it here is one question, asked before Redis and before Postgres:

    "is there any chance this id exists?"

It can answer "definitely not" or "probably yes". It is never wrong about
"definitely not", which is the only answer that matters when someone is asking
for five thousand ids per second that were never issued.

The cost is a fixed number of bits -- 100,000 user ids fit in roughly 117 KB at
a 1% false-positive rate -- and the price of that 1% is that one phantom id in a
hundred still reaches the database. That is the trade, and `false_positives` in
/metrics counts it happening rather than assuming it.
"""
import hashlib
import math


class BloomFilter:
    def __init__(self, expected: int, fp_rate: float = 0.01):
        expected = max(expected, 1)
        # The standard sizing: m bits for n items at false-positive rate p,
        # and k hash functions to go with it.
        self.m = max(8, int(math.ceil(-expected * math.log(fp_rate) / (math.log(2) ** 2))))
        self.k = max(1, int(round((self.m / expected) * math.log(2))))
        self.bits = bytearray((self.m + 7) // 8)
        self.n = 0
        self.expected = expected
        self.fp_rate = fp_rate

    def _positions(self, item: int):
        # Two hashes, combined into k -- Kirsch-Mitzenmacher. Cheaper than k
        # independent digests and, for these purposes, indistinguishable.
        digest = hashlib.blake2b(str(item).encode(), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1
        for i in range(self.k):
            yield (h1 + i * h2) % self.m

    def add(self, item: int) -> None:
        for pos in self._positions(item):
            self.bits[pos >> 3] |= 1 << (pos & 7)
        self.n += 1

    def __contains__(self, item: int) -> bool:
        return all(self.bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(item))

    @property
    def size_bytes(self) -> int:
        return len(self.bits)

    def describe(self) -> dict:
        return {
            "items": self.n,
            "bits": self.m,
            "size_bytes": self.size_bytes,
            "size_kib": round(self.size_bytes / 1024, 1),
            "hashes": self.k,
            "target_fp_rate": self.fp_rate,
            # What the maths predicts for this fill level, to compare against
            # the false positives actually observed under attack.
            "predicted_fp_rate": round(
                (1 - math.exp(-self.k * self.n / self.m)) ** self.k, 4
            ),
        }
