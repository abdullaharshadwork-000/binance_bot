import argparse
import os

import uvicorn

from app.logging_setup import configure_logging


def configure_paper_mode():
    """Use an isolated simulated portfolio while preserving configured markets."""
    os.environ.update({
        "MODE": "paper",
        "ALLOW_LIVE_TRADING": "false",
        "ALLOW_MULTI_SYMBOL_LIVE": "false",
        "DATABASE_PATH": "data/paper_validation.db",
        "LEARNING_PROFILE_PATH": "data/paper_validation_profile.json",
        "ENABLE_LLM_ADVISOR": "false",
        "ENABLE_ADAPTIVE_LEARNING": "false",
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the trading dashboard")
    parser.add_argument("--paper", action="store_true", help="Use an isolated simulated portfolio and public Spot prices")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"), help="Logging verbosity (DEBUG, INFO, WARNING, ERROR)")
    args = parser.parse_args()
    if args.paper:
        configure_paper_mode()
        print("Paper simulation: isolated data/paper_validation.db; exchange orders disabled")
    configure_logging(args.log_level)
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
