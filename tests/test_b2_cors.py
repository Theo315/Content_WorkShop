import copy
import getpass
import json
import warnings

from botocore.exceptions import ClientError
import pytest

from tools import configure_b2_cors as cors


BASE = {"bucket": "workshop-private", "endpoint": "https://s3.eu-central-003.backblazeb2.com",
        "region": "eu-central-003", "origins": ["https://workshop.vercel.app"]}
ARGS = ["--bucket", BASE["bucket"], "--endpoint", BASE["endpoint"], "--region", BASE["region"],
        "--origin", BASE["origins"][0]]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.fail_at = None
        self.wrong_policy = False

    def put_bucket_cors(self, **kwargs):
        self.calls.append(("put", kwargs))
        if self.fail_at == "put":
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "SETUP_SECRET https://secret.example"}}, "PutBucketCors")
        self.policy = copy.deepcopy(kwargs["CORSConfiguration"])

    def get_bucket_cors(self, **kwargs):
        self.calls.append(("get", kwargs))
        if self.fail_at == "get":
            raise OSError("SETUP_SECRET https://secret.example")
        if self.wrong_policy:
            return {"CORSRules": []}
        result = copy.deepcopy(self.policy)
        result["CORSRules"][0]["ID"] = "provider-generated-id"
        result["CORSRules"][0]["AllowedMethods"].reverse()
        return result

    def close(self):
        self.closed = True


def test_default_prints_policy_without_client_credentials_or_network(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("preview touched credentials or cloud")
    monkeypatch.setattr(cors, "setup_credentials", forbidden)
    monkeypatch.setattr(cors, "make_client", forbidden)
    monkeypatch.setattr(cors.boto3, "client", forbidden)
    monkeypatch.setattr(cors.getpass, "getpass", forbidden)
    assert cors.main(ARGS) == 0
    output = capsys.readouterr()
    rule = json.loads(output.out)["CORSRules"][0]
    assert rule["AllowedOrigins"] == BASE["origins"]
    assert rule["AllowedMethods"] == ["GET", "PUT", "HEAD"]
    assert rule["AllowedHeaders"] == ["*"] and rule["MaxAgeSeconds"] == 3600
    assert "OFFLINE POLICY PREVIEW ONLY" in output.err


@pytest.mark.parametrize("origin", [
    "*", "https://*.vercel.app", "http://workshop.vercel.app", "https://workshop.vercel.app/",
    "https://workshop.vercel.app/path", "https://workshop.vercel.app?key=SETUP_SECRET",
    "https://workshop.vercel.app#fragment", "https://user:SETUP_SECRET@workshop.vercel.app",
    "https://workshop.vercel.app:bad", "https://workshop.vercel.app:0", "https://bad host",
    "https://example.com\n", "https://-invalid.example", "https://example.com:65536",
])
def test_unsafe_origins_rejected_without_echoing_input(origin):
    with pytest.raises(cors.SetupError) as caught:
        cors.create_config(**{**BASE, "origins": [origin]})
    assert "SETUP_SECRET" not in str(caught.value)


@pytest.mark.parametrize("changes", [
    {"endpoint": "http://s3.eu-central-003.backblazeb2.com"},
    {"endpoint": "https://s3.us-west-004.backblazeb2.com"},
    {"endpoint": "https://s3.eu-central-003.backblazeb2.com.attacker.example"},
    {"endpoint": "https://secret@s3.eu-central-003.backblazeb2.com"},
    {"endpoint": "https://s3.eu-central-003.backblazeb2.com/workshop"},
    {"endpoint": "https://s3.eu-central-003.backblazeb2.com?key=SETUP_SECRET"},
    {"region": "auto"}, {"bucket": "../../other-bucket"}, {"origins": []},
])
def test_invalid_target_rejected(changes):
    with pytest.raises(cors.SetupError):
        cors.create_config(**{**BASE, **changes})


def test_repeatable_origins_normalize_and_deduplicate():
    config = cors.create_config(**{**BASE, "origins": ["https://WORKSHOP.vercel.app:443", "https://workshop.vercel.app", "https://preview.vercel.app:8443"]})
    assert config.origins == ("https://workshop.vercel.app", "https://preview.vercel.app:8443")


def test_runtime_s3_keys_never_implicitly_used(monkeypatch):
    monkeypatch.delenv("B2_SETUP_KEY_ID", raising=False)
    monkeypatch.delenv("B2_SETUP_SECRET", raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "RUNTIME_KEY")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "RUNTIME_SECRET")
    prompts = []
    responses = iter(["SETUP_ID", "SETUP_SECRET"])
    monkeypatch.setattr(cors.getpass, "getpass", lambda prompt: prompts.append(prompt) or next(responses))
    credentials = cors.setup_credentials()
    assert credentials.key_id == "SETUP_ID" and credentials.secret == "SETUP_SECRET"
    assert len(prompts) == 2 and all("masked" in prompt for prompt in prompts)
    assert "SETUP_SECRET" not in repr(credentials)


def test_partial_setup_env_is_rejected_without_falling_back_or_prompting(monkeypatch):
    monkeypatch.setattr(cors.getpass, "getpass", lambda *args: pytest.fail("should not mix credentials"))
    with pytest.raises(cors.SetupError, match="Set both"):
        cors.setup_credentials({"B2_SETUP_KEY_ID": "SETUP_ID"})


def test_unavailable_masked_input_aborts_instead_of_echoing(monkeypatch):
    def unsafe_getpass(prompt):
        warnings.warn("cannot hide input", getpass.GetPassWarning)
        pytest.fail("must abort before unmasked input")
    monkeypatch.setattr(cors.getpass, "getpass", unsafe_getpass)
    with pytest.raises(cors.SetupError, match="Masked input unavailable"):
        cors.setup_credentials({})


def test_apply_only_updates_named_bucket_with_dedicated_setup_key(monkeypatch, capsys):
    client, captured = FakeClient(), []
    monkeypatch.setenv("B2_SETUP_KEY_ID", "SETUP_ID")
    monkeypatch.setenv("B2_SETUP_SECRET", "SETUP_SECRET")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "RUNTIME_ID")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "RUNTIME_SECRET")
    monkeypatch.setattr(cors, "make_client", lambda config, credentials: captured.append((config, credentials)) or client)
    assert cors.main(ARGS + ["--apply"]) == 0
    assert captured[0][1] == cors.SetupCredentials("SETUP_ID", "SETUP_SECRET")
    assert client.calls == [("put", {"Bucket": BASE["bucket"], "CORSConfiguration": captured[0][0].policy()}),
                            ("get", {"Bucket": BASE["bucket"]})]
    assert client.closed
    output = capsys.readouterr()
    assert "REPLACES ALL" in output.err and "Revoke the setup key" in output.out
    assert "SETUP_SECRET" not in output.out + output.err and "RUNTIME_SECRET" not in output.out + output.err


@pytest.mark.parametrize("failed_operation,expected_code", [("put", "AccessDenied"), ("get", "NetworkOrSDKError")])
def test_safe_errors_close_client_and_surface_failure(monkeypatch, capsys, failed_operation, expected_code):
    client = FakeClient()
    client.fail_at = failed_operation
    monkeypatch.setattr(cors, "setup_credentials", lambda: cors.SetupCredentials("SETUP_ID", "SETUP_SECRET"))
    monkeypatch.setattr(cors, "make_client", lambda *args: client)
    assert cors.main(ARGS + ["--apply"]) == 2
    output = capsys.readouterr()
    assert client.closed and expected_code in output.err
    assert "SETUP_SECRET" not in output.out + output.err and "https://secret.example" not in output.err
    assert "updated and read back successfully" not in output.out


def test_readback_mismatch_is_not_reported_as_success(monkeypatch, capsys):
    client = FakeClient()
    client.wrong_policy = True
    monkeypatch.setattr(cors, "setup_credentials", lambda: cors.SetupCredentials("SETUP_ID", "SETUP_SECRET"))
    monkeypatch.setattr(cors, "make_client", lambda *args: client)
    assert cors.main(ARGS + ["--apply"]) == 2
    output = capsys.readouterr()
    assert "PolicyVerificationMismatch" in output.err and "CORS may have changed" in output.err


def test_real_client_factory_receives_only_explicit_setup_credentials(monkeypatch):
    captured = []
    monkeypatch.setattr(cors.boto3, "client", lambda *args, **kwargs: captured.append((args, kwargs)) or object())
    config = cors.create_config(**BASE)
    cors.make_client(config, cors.SetupCredentials("SETUP_ID", "SETUP_SECRET"))
    args, kwargs = captured[0]
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == BASE["endpoint"] and kwargs["region_name"] == BASE["region"]
    assert kwargs["aws_access_key_id"] == "SETUP_ID" and kwargs["aws_secret_access_key"] == "SETUP_SECRET"
