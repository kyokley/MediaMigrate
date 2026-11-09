import argparse

import pytest

from mediamigrate.cli import parser, positive_integer


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "no", "1001"])
def test_batch_size_requires_positive_integer(value):
    with pytest.raises(argparse.ArgumentTypeError):
        positive_integer(value)


def test_batch_limit_accepts_maximum():
    assert positive_integer("1000") == 1000


def test_cli_is_dry_run_and_acknowledgement_is_explicit():
    args = parser().parse_args(["3"])
    assert args.batch_size == 3
    assert not args.apply
    assert not args.writers_paused


def test_apply_requires_writers_acknowledgement(monkeypatch, capsys):
    from mediamigrate.cli import main
    assert main(["1", "--apply"]) == 2
    assert "--writers-paused" in capsys.readouterr().err
