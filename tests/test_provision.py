import tomllib

from meshradio.system.provision import write_config_toml


def test_write_config_escapes_special_characters(tmp_path):
    """A quote or backslash in a channel key must not brick first boot."""
    path = tmp_path / "config.toml"
    hostile_key = 'k"ey\\with"quotes'
    write_config_toml(
        path, hardware_profile="pi4", channel_key=hostile_key,
        corescope_url="https://scope.example.org",
    )
    with open(path, "rb") as f:
        cfg = tomllib.load(f)   # must parse cleanly
    assert cfg["mesh"]["channel_key"] == hostile_key
    assert cfg["mesh"]["enabled"] is True
