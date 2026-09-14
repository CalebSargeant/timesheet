"""Settings come out of a browser form and go straight into `Config`.

Without validation a posted `min_day_minutes=999999` is a 16-hour day on
somebody's official timesheet, and a posted time zone of nonsense crashes every
page load for that account. So every field is an explicit allow-list entry with a
type and a bound, and this pins that down."""
import pytest

from timesheet.config import Config
from timesheet.service import users


def test_every_setting_has_a_default():
    d = users.defaults()
    assert set(d) == {f.name for f in users.SETTINGS}


def test_an_unknown_field_is_ignored_not_stored():
    """A posted field nobody defined must not reach Config."""
    values, errors = users.clean({"min_day_minutes": 480, "is_admin": True,
                                  "__class__": "x"})
    assert values == {"min_day_minutes": 480}
    assert errors == []


def test_numbers_are_bounded():
    with pytest.raises(ValueError, match="between"):
        users.coerce("min_day_minutes", 999999)
    with pytest.raises(ValueError, match="between"):
        users.coerce("day_rollover_hour", -1)
    assert users.coerce("min_day_minutes", "450") == 450


def test_a_time_must_look_like_a_time():
    assert users.coerce("day_start", "08:30") == "08:30"
    for bad in ("8:30", "25:00", "08:60", "half past eight", ""):
        with pytest.raises(ValueError, match="08:30"):
            users.coerce("day_start", bad)


def test_a_time_zone_must_be_a_real_one():
    assert users.coerce("tz", "Europe/Amsterdam") == "Europe/Amsterdam"
    with pytest.raises(ValueError, match="time zone"):
        users.coerce("tz", "../../etc/passwd")
    with pytest.raises(ValueError, match="time zone"):
        users.coerce("tz", "Mars/Olympus_Mons")


def test_an_unknown_locale_falls_back_rather_than_failing():
    assert users.coerce("locale", "nl-NL") == "nl"
    assert users.coerce("locale", "klingon") == "en"


def test_an_email_must_look_like_one():
    assert users.coerce("manager_email", " boss@example.invalid ") == "boss@example.invalid"
    assert users.coerce("manager_email", "") == ""
    with pytest.raises(ValueError, match="email"):
        users.coerce("manager_email", "not an address")


def test_a_delivery_channel_must_be_one_we_have():
    assert users.coerce("delivery_channel", "chat") == "chat"
    with pytest.raises(ValueError, match="one of"):
        users.coerce("delivery_channel", "carrier-pigeon")


def test_workdays_parse_from_a_comma_list_and_are_bounded():
    assert users.coerce("workdays", "0,1,2") == (0, 1, 2)
    assert users.coerce("workdays", [4, 0, 0]) == (0, 4)
    with pytest.raises(ValueError, match="0 \\(Monday\\)"):
        users.coerce("workdays", "0,9")


def test_booleans_read_the_form_spellings():
    assert users.coerce("rota_enabled", "on") is True
    assert users.coerce("rota_enabled", "1") is True
    assert users.coerce("rota_enabled", "") is False


def test_one_bad_field_does_not_discard_the_whole_form():
    values, errors = users.clean({"tz": "Nowhere/Nothing", "day_start": "09:00"})
    assert values == {"day_start": "09:00"}
    assert len(errors) == 1


def test_account_settings_never_reach_the_reconstruction_config():
    """`manager_email` is not a Config field and must not be handed to one."""
    settings = users.defaults()
    settings["manager_email"] = "boss@example.invalid"
    settings["tz"] = "Europe/Amsterdam"
    for_cfg = users.for_config(settings)
    assert "manager_email" not in for_cfg
    assert for_cfg["tz"] == "Europe/Amsterdam"


def test_settings_overlay_onto_a_config():
    cfg = Config().with_settings(users.for_config({
        **users.defaults(), "tz": "Europe/Amsterdam", "locale": "nl",
        "min_day_minutes": 420, "workdays": [0, 1, 2],
    }))
    assert cfg.tz == "Europe/Amsterdam"
    assert cfg.labels.leave_project == "Verlof"
    assert cfg.min_day_minutes == 420
    assert cfg.workdays == (0, 1, 2)          # a JSON list becomes a tuple


def test_a_setting_that_no_longer_exists_does_not_break_an_old_account():
    cfg = Config().with_settings({"tz": "UTC", "a_setting_we_removed": 1})
    assert cfg.tz == "UTC"


def test_a_user_round_trips_through_json():
    u = users.User(id="github.com:1", login="alice", settings=users.defaults())
    u.settings["workdays"] = (0, 1, 2, 3, 4)
    again = users.User.from_dict(u.to_dict())
    assert again.settings["workdays"] == (0, 1, 2, 3, 4)
    assert again.login == "alice"


def test_a_user_id_pairs_the_host_with_the_account_id():
    assert users.user_id("GitHub.com", 42) == "github.com:42"
    assert users.user_id("ghe.example.invalid", "7") == "ghe.example.invalid:7"


# --- a deployment's declared defaults must reach a new account --------------
#
# These pass an explicit env dict rather than monkeypatching os.environ, because
# Windows drops TZ when spawning a process (bash sees it, Python does not) and
# the test would pass or fail depending on the developer's host. In the Linux
# container this runs in, TZ arrives normally.


def test_a_deployments_env_becomes_the_starting_values():
    """The chart calls these 'starting values for a new account'. They were
    hardcoded and ignored the environment entirely, so that comment was untrue
    and every account was created in UTC no matter what the deployment said."""
    d = users.defaults({"TZ": "Europe/Amsterdam", "LOCALE": "nl", "DAY_START": "09:00",
                        "MIN_DAY_MINUTES": "420", "ROTA_ENABLED": "true"})
    assert d["tz"] == "Europe/Amsterdam"
    assert d["locale"] == "nl"
    assert d["day_start"] == "09:00"
    assert d["min_day_minutes"] == 420
    assert d["rota_enabled"] is True


def test_an_empty_env_leaves_the_built_in_defaults():
    d = users.defaults({})
    assert d["tz"] == "UTC" and d["locale"] == "en" and d["day_start"] == "08:30"


def test_a_blank_env_var_is_not_a_value():
    assert users.defaults({"TZ": ""})["tz"] == "UTC"


def test_a_typo_in_the_environment_does_not_poison_every_account(caplog):
    """A bad value here would otherwise be written into every account ever
    created, and only show up as a crash on their first page load."""
    import logging
    with caplog.at_level(logging.WARNING):
        d = users.defaults({"TZ": "Mars/Olympus_Mons"})
    assert d["tz"] == "UTC"
    assert "ignoring TZ" in caplog.text


def test_per_person_fields_are_never_pre_set_from_the_environment():
    """An identity, a manager's address, or whether that person wants anything
    sent at all are not deployment-wide defaults. Seeding github_user in
    particular would point every new account at one person's commits."""
    for name in ("github_user", "manager_email", "manager_chat", "manager_name",
                 "delivery_channel", "delivery_enabled"):
        assert users.SETTINGS_BY_NAME[name].env is None, name
    d = users.defaults({"GITHUB_USER": "someone-else", "MANAGER_EMAIL": "x@y.invalid"})
    assert d["github_user"] == "" and d["manager_email"] == ""


def test_every_env_backed_field_is_validated_like_a_form_value():
    """Same coercion as the settings page, so the two cannot drift."""
    d = users.defaults({"MIN_DAY_MINUTES": "999999"})   # out of bounds
    assert d["min_day_minutes"] == 480
