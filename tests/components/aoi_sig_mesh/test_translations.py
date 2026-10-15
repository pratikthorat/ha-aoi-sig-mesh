"""strings.json is the source; en.json is a copy; every key the code uses exists."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "custom_components" / "aoi_sig_mesh"
STRINGS = json.loads((PACKAGE / "strings.json").read_text())


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", text))


def test_english_translation_is_a_copy_of_strings() -> None:
    assert json.loads((PACKAGE / "translations" / "en.json").read_text()) == STRINGS


@pytest.mark.parametrize(
    "path",
    [
        "config.step.user.menu_options.tuya_import",
        "config.step.user.menu_options.create",
        "config.step.user.menu_options.nrf_import",
        "config.step.create.data.name",
        "config.step.nrf_import.data.nrf_json",
        "config.error.nrf_bad_json",
        "config.error.nrf_bad_keys",
        "config.error.nrf_key_refresh",
        "config.error.nrf_no_app_key",
        "config.error.nrf_bad_address",
        "config.error.nrf_bad_iv_index",
        "config.step.disable_old.data.disable_old",
        "config.error.not_sig",
        "config.error.bad_keys",
        "config.error.bad_iv_index",
        "config.error.bad_address",
        "config.error.no_address",
        "config.abort.already_configured",
        "config.abort.no_import_source",
        "config.abort.network_gone",
        "config.abort.not_supported",
        "config.abort.node_added",
        "config.abort.address_conflict",
        "config.abort.cannot_save",
        "config.error.bad_mac",
        "config.flow_title",
        "config.step.device.data.network",
        "config.step.device.data.name",
        "config.step.device.data.role",
        "config.progress.provision",
        "config.error.provision_failed",
        "config.error.provision_failed_remote",
        "config.error.oob_required",
        "config.error.unknown",
        "config.error.already_provisioning",
        "config.abort.device_gone",
        "config.abort.device_added",
        "config.abort.half_provisioned",
        "config.abort.provision_failed",
        "config.abort.provision_failed_remote",
        "config.abort.oob_required",
        "config.abort.no_address",
        "config.abort.unknown",
        "config.abort.already_provisioning",
        "options.step.init.data.idle_timeout",
        "options.step.init.data.publish_setup",
        "config_subentries.node.initiate_flow.user",
        "config_subentries.node.initiate_flow.reconfigure",
        "config_subentries.node.step.reconfigure.data.role",
        "config_subentries.node.step.user.data.device",
        "config_subentries.node.step.user.data.role",
        "config_subentries.node.progress.provision",
        "config_subentries.node.error.provision_failed",
        "config_subentries.node.error.provision_failed_remote",
        "config_subentries.node.error.oob_required",
        "config_subentries.node.error.no_address",
        "config_subentries.node.error.cannot_save",
        "config_subentries.node.error.unknown",
        "config_subentries.node.error.already_provisioning",
        "config_subentries.node.abort.provision_failed",
        "config_subentries.node.abort.provision_failed_remote",
        "config_subentries.node.abort.oob_required",
        "config_subentries.node.abort.no_address",
        "config_subentries.node.abort.cannot_save",
        "config_subentries.node.abort.unknown",
        "config_subentries.node.abort.already_provisioning",
        "config_subentries.node.abort.no_devices",
        "config_subentries.node.abort.half_provisioned",
        "selector.add_role.options.auto",
        "config_subentries.node.abort.reconfigure_successful",
        "selector.role.options.light",
        "selector.role.options.switch",
        "entity.button.reconnect.name",
        "entity.sensor.signal_strength.name",
        "entity.sensor.connected_via.name",
        "entity.sensor.last_seen.name",
        "exceptions.not_connected.message",
        "exceptions.command_failed.message",
        "exceptions.seq_not_saved.message",
        "issues.no_proxy.description",
        "issues.keys_rejected.description",
        "issues.node_unreadable.description",
        "issues.iv_index_jump.title",
        "issues.iv_index_jump.fix_flow.step.confirm.description",
        "issues.iv_index_jump.fix_flow.abort.beacon_gone",
    ],
)
def test_every_key_the_code_uses_exists(path: str) -> None:
    node = STRINGS
    for part in path.split("."):
        node = node[part]
    assert isinstance(node, str) and node


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ("no_proxy", {"network", "minutes"}),
        ("keys_rejected", {"network"}),
        ("node_unreadable", {"network", "names"}),
    ],
)
def test_issue_placeholders_match_what_the_coordinator_passes(
    issue: str, expected: set[str]
) -> None:
    texts = STRINGS["issues"][issue]
    assert placeholders(texts["title"] + texts["description"]) == expected


def test_the_iv_index_jump_issue_is_a_fix_flow() -> None:
    """hassfest: a fixable issue has a fix_flow and no description of its own."""
    issue = STRINGS["issues"]["iv_index_jump"]
    confirm = issue["fix_flow"]["step"]["confirm"]

    assert "description" not in issue
    assert placeholders(issue["title"]) == {"network"}
    assert placeholders(confirm["title"] + confirm["description"]) == {
        "network",
        "ours",
        "theirs",
    }


def test_provisioning_texts_use_only_the_placeholders_the_flow_passes() -> None:
    for flow in (STRINGS["config_subentries"]["node"], STRINGS["config"]):
        for key in ("provision_failed", "provision_failed_remote"):
            assert placeholders(flow["error"][key]) == {"step", "detail"}
        assert placeholders(flow["abort"]["half_provisioned"]) == {"name", "step"}
        assert placeholders(flow["progress"]["provision"]) == {"name"}
    config = STRINGS["config"]
    assert placeholders(config["step"]["device"]["description"]) == {"name"}
    assert placeholders(config["abort"]["device_added"]) == {"name", "network"}
    assert placeholders(config["flow_title"]) == {"name"}


def test_exceptions_name_the_device() -> None:
    for key in ("not_connected", "command_failed", "seq_not_saved"):
        assert placeholders(STRINGS["exceptions"][key]["message"]) == {"name"}
