from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from backend import history
from tests.test_pipeline import client


@pytest.mark.parametrize("code,message", [
    ("AccessDenied", "Cannot download file, download bandwidth or transaction (Class B) cap exceeded."),
    ("download_cap_exceeded", "Usage cap exceeded."),
    ("transaction_cap_exceeded", "Transaction cap exceeded."),
])
def test_storage_daily_limit_is_identified(client, code, message):
    error = ClientError({"Error": {"Code": code, "Message": message}}, "GetObject")
    with patch.object(history, "batch_listing", side_effect=error):
        response = client.get("/api/history/batches")
    assert response.status_code == 503
    assert response.json()["code"] == "storage_daily_limit"
    assert "Backblaze" in response.json()["detail"]
    assert 0 < int(response.headers["Retry-After"]) <= 86401


def test_other_storage_errors_do_not_claim_a_daily_limit(client):
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": "PRIVATE_KEY"}}, "GetObject")
    with patch.object(history, "batch_listing", side_effect=error):
        response = client.get("/api/history/batches")
    assert response.status_code == 503
    assert "code" not in response.json()
    assert "PRIVATE_KEY" not in response.text
