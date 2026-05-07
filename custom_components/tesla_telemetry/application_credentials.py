"""application_credentials platform for tesla_telemetry.

Plugs us into HA's standard OAuth2 framework. Once registered, the user
can pick or paste a Tesla developer-app client_id/client_secret via
Settings → Devices & Services → Application Credentials and our config
flow will use HA's built-in OAuth2 flow (authorization_code +
refresh_token rotation handled by ``OAuth2Session``).

This eliminates the rotation race we hit when both ``tesla_fleet`` and
this integration shared a manually-pasted refresh_token: each integration
now has an independent grant from Tesla, refreshed and persisted by HA
core, and rotating one no longer invalidates the other's chain.
"""
from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import OAUTH_AUTHORIZE_URL, OAUTH_TOKEN_URL


async def async_get_authorization_server(
    hass: HomeAssistant,
) -> AuthorizationServer:
    return AuthorizationServer(
        authorize_url=OAUTH_AUTHORIZE_URL,
        token_url=OAUTH_TOKEN_URL,
    )
