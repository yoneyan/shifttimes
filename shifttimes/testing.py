"""`manage.py test` 用のテストランナー。

settings.py の TEST_RUNNER から参照する。
"""

from django.conf import settings
from django.test.runner import DiscoverRunner


class TestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # 本番用の PBKDF2 はテストでユーザを作るたびに数百ミリ秒かかるため、テスト中だけ高速なハッシュにする。
        # Django 自身も setup_test_environment で EMAIL_BACKEND などを同じように差し替えている
        self._original_password_hashers = settings.PASSWORD_HASHERS
        settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

    def teardown_test_environment(self, **kwargs):
        settings.PASSWORD_HASHERS = self._original_password_hashers
        super().teardown_test_environment(**kwargs)
