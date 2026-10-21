"""dataUtil.py: the DB password never reaches the log in full."""
import logging

import pytest

import dataUtil as DU


@pytest.mark.parametrize("secret, masked", [
    ("s3cretPassw0rd", "xxxxw0rd"),
    ("abcde", "xxxxbcde"),
    ("abcd", "xxxx"),       # 4 or fewer: nothing of it is shown
    ("", ""),
    (None, None),
])
def test_mask_secret(secret, masked):
    assert DU.mask_secret(secret) == masked


def test_get_DBengine_logs_masked_password(monkeypatch, caplog):
    monkeypatch.setattr(DU, "dbconn", None)
    monkeypatch.setattr(DU, "create_engine", lambda url: "engine")
    for k, v in {"DBHOST": "db.example", "DBUSER": "u", "DBPWD": "s3cretPassw0rd",
                 "DBMKTDATA": "Mkt", "DBPORT": "3306"}.items():
        monkeypatch.setenv(k, v)
    with caplog.at_level(logging.INFO):
        assert DU.get_DBengine() == "engine"
    assert "s3cretPassw0rd" not in caplog.text
    assert "mysql+pymysql://u:xxxxw0rd@db.example:3306/Mkt" in caplog.text
