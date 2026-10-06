"""The config file is checked before anything runs on it.

TOML parses what it's given, and a value that parses isn't one the radio can
run on: ``interval_s = -5`` made asyncio.sleep return at once (a hot loop
against the analyzer), and ``poll_interval_s = "180"`` crashed the poller,
which the supervisor restarted with a traceback every 30 seconds forever.
"""

import logging

import pytest

from meshradio import cli as cli_mod
from meshradio.config import Config, ConfigError, load_config, validate_config


def write(tmp_path, text):
    path = tmp_path / "meshradio.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_valid():
    validate_config(Config())


def test_file_values_apply_and_env_tokens_win(tmp_path, monkeypatch):
    path = write(tmp_path, """
hardware_profile = "lite"
data_dir = "/tmp/mr"

[corescope]
poll_interval_s = 60

[web]
ingest_token = "from-file"
allowed_hosts = ["meshradio.local"]

[relay]
push_url = "https://host.example"
token = "from-file"
""")
    monkeypatch.setenv("MESHRADIO_INGEST_TOKEN", "receiver-env")
    monkeypatch.setenv("MESHRADIO_RELAY_TOKEN", "pusher-env")
    cfg = load_config(path)
    assert cfg.hardware_profile == "lite" and str(cfg.data_dir) == "/tmp/mr"
    assert cfg.corescope.poll_interval_s == 60
    assert cfg.web.allowed_hosts == ["meshradio.local"]
    assert cfg.web.ingest_token == "receiver-env"
    assert cfg.relay.token == "pusher-env"


def test_public_url_env_overrides_the_file(tmp_path, monkeypatch):
    path = write(tmp_path, """
[web]
public_url = "https://meshradio.onrender.com"
""")
    assert load_config(path).web.public_url == "https://meshradio.onrender.com"
    monkeypatch.setenv("MESHRADIO_PUBLIC_URL", " https://radio.example.org ")
    assert load_config(path).web.public_url == "https://radio.example.org"
    monkeypatch.setenv("MESHRADIO_PUBLIC_URL", "")
    assert load_config(path).web.public_url == "https://meshradio.onrender.com"


def test_every_bad_value_is_reported_at_once(tmp_path):
    path = write(tmp_path, """
hardware_profile = "pi5"

[corescope]
poll_interval_s = "180"
enabled = "yes"

[player]
backend = "spotify"
timezone = "Mars/Olympus_Mons"
quiet_hours = "late"
volume = 150

[cache]
concurrency = "two"
audio_format = "../../etc/passwd"
max_bytes = true

[web]
port = 70000
allowed_hosts = "meshradio.local"

[relay]
interval_s = -5
""")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    for fragment in (
        "hardware_profile", "[corescope] poll_interval_s", "[corescope] enabled",
        "[player] backend", "[player] timezone", "[player] quiet_hours",
        "[player] volume", "[cache] concurrency", "[cache] audio_format",
        "[cache] max_bytes", "[web] port", "[web] allowed_hosts",
        "[relay] interval_s",
    ):
        assert fragment in message, fragment
    assert message.count("\n") == 13          # one line per problem, nothing else
    # A ConfigError is still the ValueError callers used to catch.
    assert isinstance(caught.value, ValueError)


def test_unknown_keys_and_sections_are_logged_and_ignored(tmp_path, caplog):
    path = write(tmp_path, """
poll_interval = 180

[corescope]
poll_interval = 60

[spotify]
enabled = true
""")
    with caplog.at_level(logging.WARNING, logger="meshradio.config"):
        cfg = load_config(path)
    assert cfg.corescope.poll_interval_s == 180        # the default stood
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "'poll_interval' in [corescope]" in messages
    assert "'poll_interval' in the top level" in messages
    assert "[spotify]" in messages


def test_main_stops_on_a_bad_config(tmp_path, monkeypatch, capsys):
    """Exit 2 with the problems on stderr, before any loop starts."""
    path = write(tmp_path, "[relay]\ninterval_s = -5\n")
    monkeypatch.setattr("sys.argv", ["meshradio", "--config", str(path)])
    with pytest.raises(SystemExit) as caught:
        cli_mod.main()
    assert caught.value.code == 2
    assert "[relay] interval_s must be at least 1, got -5" in capsys.readouterr().err


def test_main_checks_the_port_override_too(tmp_path, monkeypatch, capsys):
    path = write(tmp_path, "")
    monkeypatch.setattr("sys.argv", ["meshradio", "--config", str(path), "--port", "70000"])
    with pytest.raises(SystemExit) as caught:
        cli_mod.main()
    assert caught.value.code == 2
    assert "[web] port" in capsys.readouterr().err
