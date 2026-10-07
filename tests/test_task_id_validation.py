"""Task ID validation must accept the ID shapes the CardStore actually uses.

Regression test for the 2026-08-27 finding that `coord release-claim` could not
release a claim on any incident, problem or change card, because the validator
required pure hex and every ITIL-kind card carries a letter prefix.
"""

import click
import pytest
from click.testing import CliRunner

from skcapstone.cli._validators import validate_canonical_task_id, validate_task_id
from skcapstone.cli.coord import register_coord_commands


@pytest.mark.parametrize(
    "task_id",
    [
        "c969dfb8",  # bare hex, the common case
        "67921493",
        "inc-0e190b2f",  # 303 of these existed in the store
        "prb-41b9fb96",  # 3 of these
        "chg-1a2b3c4d",  # 8 of these
        "0e190b2f-2c88-a753",
    ],
)
def test_accepts_real_card_ids(task_id):
    assert validate_task_id(task_id) == task_id


@pytest.mark.parametrize(
    "task_id",
    [
        "",  # empty
        "x" * 65,  # too long
        "inc-0e19/../etc",  # traversal
        "INC-0e190b2f",  # prefix must be lowercase
        "incident-0e190b2f",  # prefix longer than three letters
        "zz-0e190b2f",  # prefix shorter than three letters
        "inc 0e190b2f",  # whitespace
        "inc-0e190b2f; rm -rf /",  # shell metacharacters
    ],
)
def test_rejects_bad_ids(task_id):
    with pytest.raises(click.BadParameter):
        validate_task_id(task_id)


@pytest.mark.parametrize("task_id", ["a8100002", "00000000", "deadbeef"])
def test_accepts_canonical_ids_for_new_cards(task_id):
    assert validate_canonical_task_id(task_id) == task_id


@pytest.mark.parametrize("task_id", ["a8100002-2", "A8100002", "inc-12345678", "1234567"])
def test_rejects_noncanonical_ids_for_new_cards(task_id):
    with pytest.raises(click.BadParameter):
        validate_canonical_task_id(task_id)


def test_coord_create_rejects_noncanonical_explicit_id(tmp_path):
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a8100002-2",
            "--title",
            "[M] Reject malformed new IDs",
            "--criteria",
            "The create command refuses noncanonical IDs.",
        ],
    )

    assert result.exit_code != 0
    assert "exactly eight lowercase hexadecimal characters" in result.output
