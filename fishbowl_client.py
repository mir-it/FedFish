import requests

import config


class FishbowlError(Exception):
    pass


class FishbowlClient:
    def __init__(self, host: str, port: str, timeout: int = 30):
        self.base_url = f"http://{host}:{port}/api"
        self.timeout = timeout
        self.token: str | None = None
        self._session = requests.Session()

    def login(self, username: str, password: str) -> str:
        payload = {
            "appName": config.APP_NAME,
            "appDescription": config.APP_DESCRIPTION,
            "appId": config.APP_ID,
            "username": username,
            "password": password,
        }
        resp = self._session.post(
            f"{self.base_url}/login",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise FishbowlError(f"Login failed ({resp.status_code}): {resp.text}")
        token = resp.json().get("token")
        if not token:
            raise FishbowlError("Login response missing token")
        self.token = token
        return token

    def data_query(self, sql: str) -> list[dict]:
        if not self.token:
            raise FishbowlError("Not logged in")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/sql",
        }
        resp = self._session.get(
            f"{self.base_url}/data-query",
            data=sql,
            headers=headers,
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise FishbowlError(f"data-query failed ({resp.status_code}): {resp.text}")
        try:
            rows = resp.json()
        except ValueError as e:
            raise FishbowlError(f"Invalid JSON from data-query: {e}")
        return rows or []

    def logout(self) -> None:
        if not self.token:
            return
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            self._session.post(
                f"{self.base_url}/logout", headers=headers, timeout=self.timeout
            )
        finally:
            self.token = None
