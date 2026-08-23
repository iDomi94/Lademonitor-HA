"""Konstanten für die Lademonitor-Integration."""

from datetime import timedelta

DOMAIN = "lademonitor"

CONF_BASE_URL = "base_url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_TOKEN = "token"

DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"

SERVICE_PUSH_CHARGING_SESSION = "push_charging_session"
