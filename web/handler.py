"""The request handler, assembled from its parts."""

from http.server import BaseHTTPRequestHandler

from api.routes import ApiMixin
from web.actions.dispatcher import ActionsMixin
from web.auth_pages import AuthPagesMixin
from web.base import HandlerBase
from web.downloads import DownloadsMixin
from web.router import RouterMixin

class PayrollHandler(RouterMixin, ActionsMixin, ApiMixin, DownloadsMixin, AuthPagesMixin, HandlerBase, BaseHTTPRequestHandler):
    """Every web page, form and mobile API request passes through here."""
