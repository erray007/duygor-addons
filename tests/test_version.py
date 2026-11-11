import pytest

from duygor_registry import version


def test_version_compare():
    assert version.compare("1.2", "1.2.0") == 0
    assert version.compare("1.2.0.0", "1.2") == 0
    assert version.compare("1.10", "1.9") > 0
    assert version.compare("1.9", "1.10") < 0
    assert version.compare("2", "1.9.9.9") > 0
    assert version.compare("1.9.9.9", "2") < 0
    assert version.compare("999999999", "999999998") > 0


def test_is_valid_accepts():
    for v in ["1", "1.2.3.4", "123456789.0", "0", "007"]:
        assert version.is_valid(v), v


def test_is_valid_rejects():
    for v in ["1.2.3.4.5", "v1.0", "", None, "1..2", "1.2.", "1.0-beta", "1234567890", "1.0\n", " 1.0", "١.0", "1,0"]:
        assert not version.is_valid(v), v


def test_compare_invalid_raises():
    with pytest.raises(ValueError):
        version.compare("abc", "1.0")


def test_is_newer():
    assert version.is_newer("1.1", "1.0-beta")
    assert version.is_newer("1.0", None)
    assert not version.is_newer("1.0", "1.0")
    assert not version.is_newer("1.0", "1.1")
    assert not version.is_newer("abc", "1.0")
    assert not version.is_newer(None, "1.0")
    assert not version.is_newer("abc", "xyz")
