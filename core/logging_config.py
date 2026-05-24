import logging

from pythonjsonlogger import jsonlogger


def configure_logging() -> None:
    if getattr(logging.root, "_pivot_json_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"
    ))
    logging.root.setLevel(logging.INFO)
    logging.root.addHandler(handler)
    logging.root._pivot_json_configured = True
