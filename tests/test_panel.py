"""Front panel selection: the log stand-in is for local dev, not public hosting."""

from meshradio.ui.panel import LogPanel, make_panel


def test_dev_profile_gets_the_log_panel_by_default(bus):
    assert isinstance(make_panel("dev", bus, None, None), LogPanel)


def test_embed_hosting_gets_no_panel(bus):
    assert make_panel("dev", bus, None, None, dev_log=False) is None


def test_appliance_without_oled_still_falls_back_to_the_log_panel(bus):
    # No gpiozero/luma here, so OledPanel init fails — the device keeps a log.
    assert isinstance(make_panel("pi4", bus, None, None, dev_log=False), LogPanel)
