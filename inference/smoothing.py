class ConfidenceSmoother:
    def __init__(self, class_names: list[str], alpha: float = 0.3):
        self.class_names = class_names
        self.alpha = alpha
        self._ema: dict[str, float] | None = None

    def update(self, probs: dict[str, float]) -> dict[str, float]:
        if self._ema is None:
            self._ema = dict(probs)
        else:
            self._ema = {
                c: self.alpha * probs[c] + (1 - self.alpha) * self._ema[c]
                for c in self.class_names
            }
        return dict(self._ema)

    def top_class(self, smoothed: dict[str, float]) -> tuple[str, float]:
        return max(smoothed.items(), key=lambda kv: kv[1])

    def reset(self):
        self._ema = None
