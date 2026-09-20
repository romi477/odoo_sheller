"""The one thing the daemon says about its own exposure.

A warning that is false in one of the two supported modes is worse than no
warning: it teaches the reader to scroll past the line that matters.
"""

from odoo_sheller.__main__ import IN_CONTAINER_ENV, bind_warning


def test_loopback_on_a_host_says_nothing():
    assert bind_warning("127.0.0.1", in_container=False) is None


def test_a_wider_bind_on_a_host_warns():
    message = bind_warning("0.0.0.0", in_container=False)
    assert message is not None
    assert "beyond this machine" in message
    assert "0.0.0.0" in message


def test_a_container_is_told_where_its_boundary_actually_is():
    """0.0.0.0 inside a container is required, not dangerous. What decides
    exposure there is the published port, so that is what the text names."""
    message = bind_warning("0.0.0.0", in_container=True)
    assert message is not None
    assert "beyond this machine" not in message
    assert "127.0.0.1:8765:8765" in message


def test_the_container_note_does_not_depend_on_the_bind_address():
    """The entrypoint always passes 0.0.0.0; a loopback bind there would be a
    misconfiguration, and staying silent about it would hide it."""
    assert bind_warning("127.0.0.1", in_container=True) is not None


def test_the_env_var_name_is_the_one_the_image_sets():
    assert IN_CONTAINER_ENV == "ODOO_SHELLER_IN_CONTAINER"
