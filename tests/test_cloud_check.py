"""Deployment checks are offline unless an explicit --cloud is supplied."""
import io
from types import SimpleNamespace
import urllib.error

from botocore.exceptions import ClientError
import pytest

from tools import check_cloud_storage as check


@pytest.fixture
def configured_env(monkeypatch):
    monkeypatch.delenv("APP_PUBLIC_ACCESS", raising=False)
    values = {
        "S3_BUCKET": "deployment-check-test", "S3_REGION": "auto",
        "S3_ENDPOINT_URL": "https://test.r2.cloudflarestorage.com",
        "S3_ACCESS_KEY_ID": "PRIVATE_ACCESS_KEY", "S3_SECRET_ACCESS_KEY": "PRIVATE_SECRET_KEY",
        "APP_PASSWORD": "PRIVATE_PASSWORD_12345", "SESSION_SECRET": "PRIVATE_SESSION_SECRET_12345678901234567890",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


class FakeClient:
    def __init__(self):
        self.objects = {"library/files/keep.png": b"user data"}
        self.operations = []
        self.closed = False
        self.fail_at = None
        self.fail_deletes = False
        self.bad_body = False
        self.body = None

    def call(self, operation, args):
        self.operations.append((operation, args))
        if self.fail_at == operation:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "PRIVATE_SECRET_KEY"}}, operation)

    def put_object(self, **args):
        self.call("put", args)
        self.objects[args["Key"]] = args["Body"]

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.call("sign", Params)
        assert operation == "put_object" and ExpiresIn == 600
        assert Params["ContentLength"] == check.UPLOAD_BYTES
        self.upload_key = Params["Key"]
        return "https://signed.example/PRIVATE_ACCESS_KEY?signature=PRIVATE_SECRET_KEY"

    def http_put(self, request, *, timeout):
        assert timeout == 45 and request.method == "PUT"
        assert request.get_header("Content-type") == check.CONTENT_TYPE
        assert int(request.get_header("Content-length")) == len(request.data) == 6 * 1024 * 1024
        self.objects[self.upload_key] = request.data
        return FakeResponse()

    def head_object(self, **args):
        self.call("head", args)
        return {"ContentLength": len(self.objects[args["Key"]]), "ContentType": check.CONTENT_TYPE}

    def get_object(self, **args):
        self.call("get", args)
        self.body = io.BytesIO(b"wrong" if self.bad_body else self.objects[args["Key"]])
        return {"Body": self.body}

    def copy_object(self, **args):
        self.call("copy", args)
        self.objects[args["Key"]] = self.objects[args["CopySource"]["Key"]]

    def list_objects_v2(self, **args):
        self.call("list", args)
        # Include an unrelated user object: it must never become a delete target.
        return {"Contents": [{"Key": key} for key in self.objects]}

    def delete_object(self, **args):
        self.operations.append(("delete", args))
        if self.fail_deletes:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "PRIVATE_SECRET_KEY"}}, "DeleteObject")
        self.objects.pop(args["Key"], None)

    def close(self):
        self.closed = True


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def attach_fake(monkeypatch, client):
    monkeypatch.setattr(check, "make_client", lambda config: client)
    monkeypatch.setattr(check.urllib.request, "urlopen", client.http_put)
    monkeypatch.setattr(check.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))


def assert_private_values_absent(output, values):
    for name in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "APP_PASSWORD", "SESSION_SECRET"):
        assert values[name] not in output
    assert "https://signed.example" not in output


def test_default_never_constructs_client_or_requests_network(configured_env, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("offline check attempted a cloud operation")
    monkeypatch.setattr(check, "make_client", forbidden)
    monkeypatch.setattr(check.boto3, "client", forbidden)
    monkeypatch.setattr(check.urllib.request, "urlopen", forbidden)
    assert check.main([]) == 0
    output = capsys.readouterr().out
    assert "OFFLINE CONFIGURATION CHECK PASSED" in output and "does NOT validate" in output
    assert_private_values_absent(output, configured_env)


@pytest.mark.parametrize("field,value", [
    ("APP_PASSWORD", "short"), ("SESSION_SECRET", "short"), ("S3_BUCKET", ""),
    ("S3_ACCESS_KEY_ID", ""), ("S3_SECRET_ACCESS_KEY", ""),
    ("S3_ENDPOINT_URL", "http://insecure.example"),
    ("S3_ENDPOINT_URL", "https://PRIVATE_ACCESS_KEY:PRIVATE_SECRET_KEY@example.com"),
    ("S3_ENDPOINT_URL", "https://example.com/bucket?signature=PRIVATE_SECRET_KEY"),
    ("S3_REGION", ""),
])
def test_invalid_config_blocks_cloud_before_network(field, value, configured_env, monkeypatch, capsys):
    monkeypatch.setenv(field, value)
    monkeypatch.setattr(check, "make_client", lambda config: pytest.fail("invalid config reached cloud"))
    assert check.main(["--cloud"]) == 1
    output = capsys.readouterr().out
    assert field in output and "No cloud requests" in output
    assert_private_values_absent(output, configured_env)


def test_aws_default_requires_an_explicit_region(configured_env):
    configured_env["S3_ENDPOINT_URL"] = ""
    with pytest.raises(check.ConfigurationError, match="S3_REGION"):
        check.configuration(configured_env)
    configured_env["S3_REGION"] = "eu-central-1"
    config = check.configuration(configured_env)
    assert config.endpoint is None and config.region == "eu-central-1"


def test_public_configuration_does_not_require_password(configured_env):
    configured_env["APP_PUBLIC_ACCESS"] = "true"
    configured_env.pop("APP_PASSWORD")
    assert check.configuration(configured_env).bucket == configured_env["S3_BUCKET"]
    configured_env["SESSION_SECRET"] = ""
    with pytest.raises(check.ConfigurationError, match="SESSION_SECRET"):
        check.configuration(configured_env)


def test_explicit_cloud_exercises_large_signed_upload_and_cleans_exact_own_keys(configured_env, monkeypatch, capsys):
    client = FakeClient()
    attach_fake(monkeypatch, client)
    assert check.main(["--cloud"]) == 0
    assert client.objects == {"library/files/keep.png": b"user data"} and client.closed
    deletions = [args["Key"] for operation, args in client.operations if operation == "delete"]
    prefix = "temporary/deploy-check/" + "a" * 32 + "/"
    assert deletions == [prefix + "upload.bin", prefix + "copy.bin"]
    listings = [args for operation, args in client.operations if operation == "list"]
    assert listings == [{"Bucket": configured_env["S3_BUCKET"], "Prefix": prefix, "MaxKeys": 10}]
    assert client.body.closed
    output = capsys.readouterr().out
    assert "CLOUD CHECK PASSED" in output and "CORS" in output and "remain unverified" in output
    assert_private_values_absent(output, configured_env)


@pytest.mark.parametrize("failed_operation", ["put", "sign", "head", "get", "copy", "list"])
def test_sdk_failures_always_attempt_scoped_cleanup(configured_env, monkeypatch, capsys, failed_operation):
    client = FakeClient()
    client.fail_at = failed_operation
    attach_fake(monkeypatch, client)
    assert check.main(["--cloud"]) == 2
    assert client.objects == {"library/files/keep.png": b"user data"} and client.closed
    deletions = [args["Key"] for operation, args in client.operations if operation == "delete"]
    assert len(deletions) == 2 and all(key.startswith("temporary/deploy-check/" + "a" * 32 + "/") for key in deletions)
    output = capsys.readouterr().out
    assert "AccessDenied" in output and "CLOUD CHECK FAILED" in output
    assert_private_values_absent(output, configured_env)


def test_signed_http_errors_do_not_leak_url_or_headers(configured_env, monkeypatch, capsys):
    client = FakeClient()
    attach_fake(monkeypatch, client)
    def failed_put(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 403, "PRIVATE_SECRET_KEY", {"Secret": "PRIVATE_ACCESS_KEY"}, None)
    monkeypatch.setattr(check.urllib.request, "urlopen", failed_put)
    assert check.main(["--cloud"]) == 2
    output = capsys.readouterr().out
    assert "signed-http-put-6MiB: HTTP_403" in output
    assert_private_values_absent(output, configured_env)
    assert client.objects == {"library/files/keep.png": b"user data"}


def test_corrupt_download_closes_stream_and_cleans_up(configured_env, monkeypatch, capsys):
    client = FakeClient()
    client.bad_body = True
    attach_fake(monkeypatch, client)
    assert check.main(["--cloud"]) == 2
    assert client.body.closed and client.objects == {"library/files/keep.png": b"user data"}
    assert "ObjectContentMismatch" in capsys.readouterr().out


def test_cleanup_failure_is_visible_and_never_reports_success(configured_env, monkeypatch, capsys):
    client = FakeClient()
    client.fail_deletes = True
    attach_fake(monkeypatch, client)
    assert check.main(["--cloud"]) == 2
    output = capsys.readouterr().out
    assert output.count("CLEANUP ERROR") == 2 and "Cleanup was incomplete" in output
    assert "CLOUD CHECK PASSED" not in output and client.closed
    assert_private_values_absent(output, configured_env)


def test_untrusted_provider_error_code_is_not_printed():
    error = ClientError({"Error": {"Code": "https://example.com/PRIVATE_SECRET_KEY", "Message": "secret"}}, "PutObject")
    assert check.safe_error_code(error) == "ProviderError"
