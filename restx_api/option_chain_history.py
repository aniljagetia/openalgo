"""
Option Chain Historical OI API Endpoint

POST /api/v1/optionchain/historical-oi

Companion endpoint to /api/v1/optionchain. Returns the closing OI from
a 1-minute broker-historical candle at `now - lookback_minutes` ago for
each requested option symbol. The frontend OptionChain page uses this
to back the Build Up / Trend classifier with true historical OI data
(matching the user's chosen refresh interval) instead of the previous
in-memory poll snapshot, which only worked once both polls had landed.

Request Body:
{
    "apikey": "your_api_key",
    "symbols": [
        {"symbol": "NIFTY30DEC2524000CE", "exchange": "NFO"},
        {"symbol": "NIFTY30DEC2524000PE", "exchange": "NFO"}
    ],
    "lookback_minutes": 15
}

Response:
{
    "status": "success",
    "data": {
        "NIFTY30DEC2524000CE": {
            "prev_oi": 1234500,
            "candle_ts": "2026-06-26T10:15:00"
        },
        ...
    }
}
"""

import os

from flask import request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from limiter import limiter
from services.option_chain_history_service import get_historical_oi_at_lookback
from utils.logging import get_logger

from .data_schemas import OptionChainHistoricalOiSchema

logger = get_logger(__name__)

api = Namespace(
    "optionchain_history",
    description="Get historical OI per symbol for Build Up / Trend classification",
)

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")


@api.route("/historical-oi", strict_slashes=False)
class OptionChainHistoricalOi(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Return historical OI snapshot from `lookback_minutes` ago per symbol."""
        try:
            schema = OptionChainHistoricalOiSchema()
            data = schema.load(request.json)

            api_key = data["apikey"]
            symbols = data["symbols"]
            lookback_minutes = data["lookback_minutes"]

            logger.info(
                f"OI history request: {len(symbols)} symbols, lookback={lookback_minutes}min"
            )

            _, response, status_code = get_historical_oi_at_lookback(
                symbols=symbols,
                lookback_minutes=lookback_minutes,
                api_key=api_key,
            )
            return response, status_code

        except ValidationError as err:
            logger.warning(f"Validation error in OI history request: {err.messages}")
            return {"status": "error", "message": "Validation error", "errors": err.messages}, 400
        except Exception as e:
            logger.exception(f"Unexpected error in OI history endpoint: {e}")
            return {"status": "error", "message": "An unexpected error occurred"}, 500
