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
SERVICE_BEGIN_CHARGING_SESSION = "begin_charging_session"
SERVICE_END_CHARGING_SESSION = "end_charging_session"
