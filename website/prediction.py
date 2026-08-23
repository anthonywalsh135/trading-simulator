#fits a straight line through the recent prices and carries it forwards, using
#the linear regression model from scikit learn.
#
#a straight line drawn through noisy prices looks exactly as convincing as a
#real trend, so this also reports R squared, which is how much of the movement
#in the price the line actually accounts for, and a band around the projection
#worked out from how far the real prices sat from the line. the chart can then
#show the user how much to trust what it is drawing.

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LinearRegression

from .market import Candle


@dataclass
class Prediction:
    timestamps: list[int]
    prices: list[float]
    upper: list[float]
    lower: list[float]
    r_squared: float
    slope_per_hour: float
    confidence: str  #high, moderate or low
    warning: str | None

    def to_dict(self) -> dict:
        return {
            "timestamps": self.timestamps,
            "prices": self.prices,
            "upper": self.upper,
            "lower": self.lower,
            "r_squared": self.r_squared,
            "slope_per_hour": self.slope_per_hour,
            "confidence": self.confidence,
            "warning": self.warning,
        }


def predict(candles: list[Candle], steps: int = 12) -> Prediction | None:
    """Fit a line through the bars and carry it forward.

    returns None when there is not enough data for the fit to mean anything.
    """
    if len(candles) < 10:
        return None

    timestamps = np.array([c.ts for c in candles], dtype=np.float64)
    closes = np.array([c.close for c in candles], dtype=np.float64)

    #fit against seconds since the first bar rather than raw timestamps. a
    #timestamp is around 1.8 billion, which is so large next to the price that
    #the maths behind the fit loses accuracy.
    elapsed = timestamps - timestamps[0]

    model = LinearRegression()
    model.fit(elapsed.reshape(-1, 1), closes)

    fitted = model.predict(elapsed.reshape(-1, 1))
    residuals = closes - fitted
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((closes - closes.mean()) ** 2))
    r_squared = round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else 0.0

    #how far the real prices sat from the line, used as the width of the band
    std_error = float(np.std(residuals))

    bar_seconds = float(np.median(np.diff(timestamps))) if len(timestamps) > 1 else 300.0
    future_elapsed = np.array(
        [elapsed[-1] + bar_seconds * (i + 1) for i in range(steps)], dtype=np.float64
    )
    future_prices = model.predict(future_elapsed.reshape(-1, 1))

    #the further ahead the projection goes, the wider the band gets
    widths = np.array([std_error * (1 + 0.35 * (i + 1)) for i in range(steps)])

    if r_squared >= 0.7:
        confidence, warning = "high", None
    elif r_squared >= 0.35:
        confidence, warning = "moderate", "The trend explains only part of the price movement."
    else:
        confidence, warning = (
            "low",
            "This price is close to random over the period shown, so the projection "
            "is not a reliable forecast.",
        )

    return Prediction(
        timestamps=[int(timestamps[0] + e) for e in future_elapsed],
        prices=[round(float(p), 4) for p in future_prices],
        upper=[round(float(p + w), 4) for p, w in zip(future_prices, widths)],
        lower=[round(float(max(p - w, 0)), 4) for p, w in zip(future_prices, widths)],
        r_squared=r_squared,
        slope_per_hour=round(float(model.coef_[0]) * 3600, 4),
        confidence=confidence,
        warning=warning,
    )
