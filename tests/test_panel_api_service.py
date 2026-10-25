"""PanelApiService against the Remnawave 3.4.x contract (numeric user id, /users/stream, A025)."""
import pytest

from bot.services.panel_api_service import PanelApiService


class FakeRequest:
    """Replaces PanelApiService._request: records calls, returns queued responses."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    async def __call__(self, method, endpoint, log_full_response=False, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {endpoint}")
        return self.responses.pop(0)


@pytest.fixture
def api(settings):
    return PanelApiService(settings)


def install(api, responses):
    fake = FakeRequest(responses)
    api._request = fake
    return fake


def not_found(code="A025"):
    return {"error": True, "status_code": 404,
            "details": {"message": "User not found", "errorCode": code}}


async def test_telegram_lookup_uses_stream_and_follows_cursor(api):
    fake = install(api, [
        {"response": {"users": [{"id": 1, "telegramId": 42}, {"id": 2, "telegramId": 4242}],
                      "nextCursor": 7, "hasMore": True}},
        {"response": {"users": [{"id": 3, "telegramId": 42}], "nextCursor": None, "hasMore": False}},
    ])
    users = await api.get_users_by_filter(telegram_id=42)
    assert [u["id"] for u in users] == [1, 3]  # non-matching telegramId filtered out
    assert [c[:2] for c in fake.calls] == [("GET", "/users/stream"), ("GET", "/users/stream")]
    first, second = fake.calls[0][2]["params"], fake.calls[1][2]["params"]
    assert first["telegramId"] == "42" and "cursor" not in first
    assert second["cursor"] == 7


async def test_telegram_lookup_empty(api):
    install(api, [{"response": {"users": [], "nextCursor": None, "hasMore": False}}])
    assert await api.get_users_by_filter(telegram_id=1) == []


@pytest.mark.parametrize("resp", [not_found("A025"), not_found("A062"),
                                  {"error": True, "status_code": 404, "details": {}}])
async def test_telegram_lookup_not_found_is_empty_list(api, resp):
    install(api, [resp])
    assert await api.get_users_by_filter(telegram_id=1) == []


async def test_telegram_lookup_api_failure_is_none(api):
    install(api, [{"error": True, "status_code": 500, "details": {}}])
    assert await api.get_users_by_filter(telegram_id=1) is None


async def test_username_lookup(api):
    fake = install(api, [{"response": {"id": 5, "username": "tg_1"}}])
    assert await api.get_users_by_filter(username="tg_1") == [{"id": 5, "username": "tg_1"}]
    assert fake.calls[0][:2] == ("GET", "/users/by-username/tg_1")


@pytest.mark.parametrize("resp", [not_found("A025"), not_found("A062")])
async def test_username_lookup_not_found(api, resp):
    install(api, [resp])
    assert await api.get_users_by_filter(username="nobody") == []


async def test_get_user_by_panel_id(api):
    fake = install(api, [{"response": {"id": 9}}])
    assert await api.get_user_by_panel_id(9) == {"id": 9}
    assert fake.calls[0][:2] == ("GET", "/users/9")


async def test_update_user_details_sends_numeric_id_not_uuid(api):
    fake = install(api, [{"response": {"id": 12, "status": "ACTIVE"}}])
    result = await api.update_user_details_on_panel("12", {"expireAt": "2030-01-01T00:00:00.000Z"})
    assert result == {"id": 12, "status": "ACTIVE"}
    method, endpoint, kwargs = fake.calls[0]
    assert (method, endpoint) == ("PATCH", "/users")
    assert kwargs["json"]["id"] == 12 and isinstance(kwargs["json"]["id"], int)
    assert "uuid" not in kwargs["json"]


async def test_update_user_details_failure_returns_none(api):
    install(api, [{"error": True, "status_code": 400, "details": {}}])
    assert await api.update_user_details_on_panel(1, {"description": "x"}) is None


@pytest.mark.parametrize("enable,action,status", [(True, "enable", "ACTIVE"), (False, "disable", "DISABLED")])
async def test_update_user_status_endpoint(api, enable, action, status):
    fake = install(api, [{"response": {"id": 3, "status": status}}])
    assert await api.update_user_status_on_panel(3, enable) is True
    assert fake.calls[0][:2] == ("POST", f"/users/3/actions/{action}")


async def test_delete_empty_2xx_is_success(api):
    fake = install(api, [{"status": "success", "code": 200, "data_text": ""}])
    assert await api.delete_user_from_panel(4) is True
    assert fake.calls[0][:2] == ("DELETE", "/users/4")


@pytest.mark.parametrize("resp", [not_found("A025"), not_found("A062")])
async def test_delete_not_found_is_already_deleted(api, resp):
    install(api, [resp])
    assert await api.delete_user_from_panel(4) is True


@pytest.mark.parametrize("resp", [None, {"error": True, "status_code": 500,
                                         "details": {"errorCode": "A040"}}])
async def test_delete_other_errors_fail(api, resp):
    install(api, [resp])
    assert await api.delete_user_from_panel(4) is False


async def test_get_user_devices_endpoint(api):
    fake = install(api, [{"response": {"total": 1, "devices": [{"hwid": "h"}]}}])
    assert (await api.get_user_devices(8))["total"] == 1
    assert fake.calls[0][:2] == ("GET", "/hwid/devices/8")


async def test_disconnect_device_body_uses_user_id(api):
    fake = install(api, [{"response": {"total": 0, "devices": []}}])
    assert await api.disconnect_device("8", "hw-1") is True
    method, endpoint, kwargs = fake.calls[0]
    assert (method, endpoint) == ("POST", "/hwid/devices/delete")
    assert kwargs["json"] == {"userId": 8, "hwid": "hw-1"}


async def test_encrypt_happ_link_is_disabled(api):
    fake = install(api, [])
    assert await api.encrypt_happ_link("https://x") is None
    assert fake.calls == []
