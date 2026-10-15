"""Shared constants and helpers for unit/integration tests."""

CLIENT_ID = "123456789012345678"
OTHER_ID = "876543210987654321"
BASE_URL = "http://127.0.0.1:8765"
WS_URL = "ws://127.0.0.1:8765/api/v1/ws"


GOOD_ACTIVITY = {
    "details": "Coding Session",
    "state": "Visual Studio Code",
    "large_image": "vscode",
    "large_text": "Writing Code",
    "buttons": [{"label": "GitHub", "url": "https://github.com/Callairen"}],
}


def configure(client, auth, client_id: str = CLIENT_ID, activity: dict | None = None):
    r = client.post("/api/v1/setup/complete", headers=auth,
                    json={"client_id": client_id, "activity": activity or GOOD_ACTIVITY})
    assert r.status_code == 200, r.text
    return r
