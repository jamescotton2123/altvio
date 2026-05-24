import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from supabase import Client, create_client

env_path = Path(__file__).resolve().parent.parent / ".env"
_log = logging.getLogger(__name__)
_log.debug("Loading .env from %s", env_path)
load_dotenv(dotenv_path=env_path)
_log.debug("SUPABASE_URL present: %s", os.getenv('SUPABASE_URL') is not None)

SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY: str = os.environ.get("SUPABASE_KEY", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def test_connection() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError("SUPABASE_URL and SUPABASE_KEY must be set in your .env file.")
    _log.info("Connection Success: Altvio Database is Live")


def fetch_investors():
    try:
        response = supabase.table("investors").select("*").execute()
        return response.data
    except Exception as e:
        _log.error("Error fetching investors: %s", e)


if __name__ == "__main__":
    test_connection()
    investors = fetch_investors()
    _log.info("Investors: %s", investors)
