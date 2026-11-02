import unittest

from sqlalchemy.engine import make_url

from crashclouseau import config


class TestSqlalchemyUrl(unittest.TestCase):
    def test_heroku_scheme_uses_psycopg2(self):
        url = config.sqlalchemy_url("postgres://u:p@host:5432/db")
        self.assertEqual(url, "postgresql+psycopg2://u:p@host:5432/db")
        self.assertEqual(make_url(url).get_driver_name(), "psycopg2")

    def test_bare_postgresql_uses_psycopg2(self):
        # SQLAlchemy 2.1 maps a bare postgresql:// to psycopg 3, which is not installed.
        url = config.sqlalchemy_url("postgresql://u:p@host/db")
        self.assertEqual(make_url(url).get_driver_name(), "psycopg2")

    def test_explicit_driver_and_other_databases_are_kept(self):
        for url in ("postgresql+psycopg://u@h/db", "postgresql+psycopg2://u@h/db", "sqlite://"):
            self.assertEqual(config.sqlalchemy_url(url), url)


if __name__ == "__main__":
    unittest.main()
