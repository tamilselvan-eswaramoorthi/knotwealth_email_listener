import os

class Config:
    DB_HOST = os.getenv('DB_HOST', '').strip()
    DB_PORT = os.getenv('DB_PORT', '1433').strip()
    DB_NAME = os.getenv('DB_NAME', '').strip()
    DB_USER = os.getenv('DB_USER', '').strip()
    DB_PASSWORD = os.getenv('DB_PASSWORD', '').strip()
    DB_SCHEMA = os.getenv('DB_SCHEMA', 'dbo').strip()

    USER_EMAIL = os.getenv("USER_EMAIL", "")
    TRANSACTIONS_USER_ID = os.getenv("TRANSACTIONS_USER_ID", "ZdN31Wb3GLY36wZPUcBDrP3Qmbw1")

    # Shared secret the Apps Script poller sends in the X-API-Key header
    API_KEY = os.getenv("API_KEY", "").strip()
