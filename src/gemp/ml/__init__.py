"""Forecasting and anomaly detection.

Both exist to serve the optimizer rather than to stand alone:

* the forecast supplies the annualized consumption estimate the savings model needs
  (F3), which is the only point at which the time series actually reaches the
  allocation decision;
* anomaly detection surfaces buildings whose consumption is drifting away from their
  own expected behaviour, which is operational information a portfolio manager acts
  on directly.
"""
