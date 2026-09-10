"""Intraday option-selling decision support.

Answers one question the Nifty Bias Dashboard deliberately does not: given
where the market is *right now*, is the better intraday trade selling a call
or selling a put -- and is it a day worth selling on at all?

Those are two separate questions and this package keeps them separate:

* :mod:`side` scores the direction (which side to sell).
* :mod:`premium` scores the regime (whether to sell anything).

Conflating them is how sellers end up short calls into a trend day.
"""
