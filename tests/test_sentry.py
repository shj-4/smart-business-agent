"""
اختبارات تكامل Sentry الاختياري — بلا تحميل حقيقي عند غياب DSN.
"""

from unittest.mock import patch


class TestInstallSentry:
    def test_noop_without_dsn(self):
        from app.sentry import install_sentry

        install_sentry.cache_clear()
        assert install_sentry() is False

    def test_initializes_when_dsn_present(self):
        from app.sentry import install_sentry

        install_sentry.cache_clear()
        with patch("app.config.settings.sentry_dsn", "https://fake@dsn.example/1"):
            with patch("sentry_sdk.init") as mock_init:
                assert install_sentry() is True
        mock_init.assert_called_once()

    def test_graceful_on_init_error(self):
        from app.sentry import install_sentry

        install_sentry.cache_clear()
        with patch("app.config.settings.sentry_dsn", "https://fake@dsn.example/1"):
            with patch("sentry_sdk.init", side_effect=RuntimeError("bad dsn")):
                assert install_sentry() is False
