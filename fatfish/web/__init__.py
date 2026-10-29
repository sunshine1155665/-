"""Local web UI and JSON API for the fatfish companion."""

from .server import APP_VERSION, FatfishHTTPServer, create_server, run_server

__all__ = ["APP_VERSION", "FatfishHTTPServer", "create_server", "run_server"]
