from timmy.winsys import winver


def test_gates():
    assert not winver.loopback_supported(18363)          # Windows 10 1909
    assert winver.loopback_supported(19041)              # 2004
    assert winver.loopback_supported(19045)              # 22H2
    assert winver.loopback_supported(26100)              # Windows 11 24H2
    assert winver.exclude_from_capture_supported(22631)
    assert not winver.exclude_from_capture_supported(0)


def test_labels_are_hebrew():
    assert winver.describe(22631) == "Windows 11"
    assert "ישן" in winver.describe(18363)
    assert winver.describe(None) == "לא ווינדוס"
