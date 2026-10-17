def test_deliberate_failure() -> None:
    raise AssertionError("deliberate CI check")
