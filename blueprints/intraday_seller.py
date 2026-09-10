"""
Intraday Option Seller Blueprint

Endpoints:
    GET /optionseller/api/snapshot  - Call-sell vs put-sell reading for NIFTY

Falls back to fixture data when no API key is configured, so the page always
renders something honest rather than erroring. The payload always reports
which source it used.
"""

from flask import Blueprint, jsonify, request, session
from flask_cors import cross_origin

from database.auth_db import get_api_key_for_tradingview
from services.intraday_seller_service import get_snapshot
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

intraday_seller_bp = Blueprint("intraday_seller_bp", __name__, url_prefix="/")


@intraday_seller_bp.route("/optionseller/api/snapshot", methods=["GET"])
@cross_origin()
@check_session_validity
def snapshot():
    """Return the current intraday option-selling reading."""
    try:
        login_username = session.get("user")
        if not login_username:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        use_mock = request.args.get("mock", "").lower() in ("1", "true", "yes")
        api_key = None if use_mock else get_api_key_for_tradingview(login_username)

        return jsonify(get_snapshot(api_key=api_key, use_mock=use_mock))
    except Exception as e:
        logger.exception(f"Error building intraday seller snapshot: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
