"""Chord-aware follow-along state independent from the UI and game input."""

from .score import Score


class Practice:
    def __init__(self, score: Score):
        self.groups = score.groups()
        self.index = 0
        self.held: set[int] = set()
        self.fresh: set[int] = set()

    @property
    def finished(self):
        return self.index >= len(self.groups)

    def expected(self, offset: int = 0) -> set[int]:
        index = self.index + offset
        return set(self.groups[index][1]) if 0 <= index < len(self.groups) else set()

    def press(self, key: int) -> bool:
        if key in self.held:
            return False
        self.held.add(key)
        self.fresh.add(key)
        expected = self.expected()
        if expected and expected <= self.held and expected <= self.fresh:
            self.index += 1
            self.fresh.clear()  # Repeated chords require releasing and pressing again.
            return True
        return False

    def release(self, key: int):
        self.held.discard(key)
        self.fresh.discard(key)

    def seek(self, index: int):
        self.index = max(0, min(len(self.groups), index))
        self.held.clear()
        self.fresh.clear()
