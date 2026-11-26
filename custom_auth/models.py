import uuid

import pyotp
from django.apps import apps
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.mail import send_mail
from django.db import models, transaction
from django.template.loader import render_to_string
from django.utils import timezone
from simple_history.models import HistoricalRecords

from custom_auth.utils import random_string


class GroupManager(models.Manager):
    def active_filter(self):
        return self.filter(is_active=True)

    def create_group(
        self,
        name,
        name_jp,
        postcode,
        address,
        address_jp,
        phone,
        country="Japan",
        contract_type="E-Mail",
        **extra_fields,
    ):
        extra_fields.setdefault("is_pass", False)
        return self.create(
            name=name,
            name_jp=name_jp,
            postcode=postcode,
            address=address,
            address_jp=address_jp,
            phone=phone,
            country=country,
            contract_type=contract_type,
            **extra_fields,
        )

    def update_group(self, group_id, postcode, address, address_en, phone, country):
        group = Group.objects.get(id=group_id)
        group.postcode = postcode
        group.address = address
        group.address_en = address_en
        group.phone = phone
        group.country = country
        group.save()


GROUP_STATUS_CHOICES = (
    (1, "有効"),
    (10, "ユーザより廃止"),
    (11, "運営委員より廃止"),
    (12, "審査落ち"),
)

# Membership
# 1-49 一般支払い
# 70-89 特別枠(無料)
# 90-99 特別枠(運営)
MEMBERSHIP_TYPE_CHOICES = (
    (1, "一般会員"),
    (40, "運営委員(優勝)"),
    (70, "学生委員"),
    (90, "運営委員(無償)"),
    (99, "その他"),
)

CONTRACT_TYPE_CHOICES = (
    ("E-Mail", "E-Mail"),
    ("AirMail", "AirMail)"),
)

# Stripe のサブスクリプションステータス
SUBSCRIPTION_STATUS_CHOICES = (
    ("", "未契約"),
    ("trialing", "トライアル中"),
    ("active", "有効"),
    ("past_due", "支払い遅延"),
    ("unpaid", "未払い"),
    ("incomplete", "決済未完了"),
    ("incomplete_expired", "決済未完了(期限切れ)"),
    ("paused", "一時停止"),
    ("canceled", "解約済み"),
)

# 機能を利用させてよいステータス。past_due は Stripe のリトライ期間中なので猶予する
ACTIVE_SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due")

# 無償付与で「人数無制限」を表すプランキー
UNLIMITED_PLAN_KEY = "unlimited"


def get_plan(plan_key):
    """プランキーからプラン定義を返す。未知のキーは無料プラン扱いにする。"""
    if plan_key == UNLIMITED_PLAN_KEY:
        return settings.UNLIMITED_PLAN
    return settings.STRIPE_PLANS.get(plan_key, settings.FREE_PLAN)


def get_plan_by_price_id(price_id):
    """Stripe の price_id から (プランキー, プラン定義) を引く。"""
    if price_id:
        for key, plan in settings.STRIPE_PLANS.items():
            if plan["price_id"] and plan["price_id"] == price_id:
                return key, plan
    return "", settings.FREE_PLAN


def free_plan_choices():
    """無償付与プランの選択肢を settings のプラン定義から組み立てる。"""
    choices = [("", "なし(通常の課金対象)")]
    choices += [
        (key, f"{plan['name']} 相当(最大{plan['max_members']}名)") for key, plan in settings.STRIPE_PLANS.items()
    ]
    choices.append((UNLIMITED_PLAN_KEY, f"{settings.UNLIMITED_PLAN['name']} 相当(人数無制限)"))
    return choices


class Group(models.Model):  # noqa: F811
    created_at = models.DateTimeField("作成日", default=timezone.now)
    updated_at = models.DateTimeField("更新日", default=timezone.now)
    name = models.CharField("name", max_length=150, unique=True)
    name_jp = models.CharField("name(japanese)", max_length=150, unique=True)
    comment = models.CharField("comment", max_length=250, default="", blank=True)
    status = models.IntegerField("ステータス", default=1, choices=GROUP_STATUS_CHOICES)
    membership_type = models.IntegerField("会員種別", default=1, choices=MEMBERSHIP_TYPE_CHOICES)
    membership_expired_at = models.DateTimeField("有効期限", blank=True, null=True)

    stripe_customer_id = models.CharField("Stripe(CusID)", max_length=200, blank=True, null=True)
    stripe_subscription_id = models.CharField("Stripe(SubID)", max_length=200, blank=True, null=True)
    # Stripe 側の契約状態のキャッシュ。Webhook と請求画面の表示時に同期する
    stripe_plan = models.CharField("契約プラン", max_length=50, default="", blank=True)
    stripe_status = models.CharField(
        "契約ステータス", max_length=50, default="", blank=True, choices=SUBSCRIPTION_STATUS_CHOICES
    )
    stripe_current_period_end = models.DateTimeField("現在の請求期間の終了日", blank=True, null=True)
    stripe_cancel_at_period_end = models.BooleanField("期間末に解約予定", default=False)

    # 無償化: 運営判断で Stripe 契約なしに有料プラン相当を付与する。
    # 期限は membership_expired_at を使う(未設定なら無期限)
    free_plan = models.CharField("無償付与プラン", max_length=50, default="", blank=True, choices=free_plan_choices)
    free_reason = models.CharField("無償付与の理由", max_length=250, default="", blank=True)

    # group personal info
    postcode = models.CharField("郵便番号", max_length=20, default="")
    address = models.CharField("住所", max_length=250, default="")
    address_jp = models.CharField("住所(Japanese)", max_length=250, default="")
    phone = models.CharField("phone", max_length=30, default="")
    country = models.CharField("居住国", max_length=100, default="Japan")
    contract_type = models.CharField("契約種別", max_length=100, default="E-Mail", choices=CONTRACT_TYPE_CHOICES)
    users = models.ManyToManyField(
        "User",
        blank=True,
        through="UserGroup",
        through_fields=("group", "user"),
        related_name="group_users_set",
    )
    history = HistoricalRecords()

    objects = GroupManager()

    class Meta:
        verbose_name = "グループ"
        verbose_name_plural = "グループ"

    def __str__(self):
        return "%s: %s" % (self.id, self.name)

    @property
    def is_free_granted(self):
        """運営により無償でプランが付与されているか(期限切れは対象外)。"""
        if not self.free_plan:
            return False
        if self.membership_expired_at and self.membership_expired_at < timezone.now():
            return False
        return True

    @property
    def has_active_subscription(self):
        """Stripe の契約が機能を利用できる状態か。"""
        return self.stripe_status in ACTIVE_SUBSCRIPTION_STATUSES

    @property
    def plan_key(self):
        """実際に適用されるプランのキー。無償付与と有料契約のうち上位を採用する。"""
        candidates = [("", settings.FREE_PLAN)]
        if self.is_free_granted:
            candidates.append((self.free_plan, get_plan(self.free_plan)))
        if self.has_active_subscription and self.stripe_plan:
            candidates.append((self.stripe_plan, get_plan(self.stripe_plan)))
        return max(candidates, key=lambda candidate: candidate[1]["rank"])[0]

    @property
    def plan(self):
        """実際に適用されるプラン定義。"""
        return get_plan(self.plan_key)

    @property
    def max_members(self):
        """メンバー数の上限。None は無制限。"""
        return self.plan["max_members"]

    @property
    def member_count(self):
        return self.usergroup_set.count()

    def can_add_member(self, count=1):
        """あと count 人メンバーを追加できるか。"""
        if self.max_members is None:
            return True
        return self.member_count + count <= self.max_members


class UserManager(BaseUserManager):
    def _create_user(self, username, username_jp, email, password, **extra_fields):
        if not username:
            raise ValueError("The given username must be set")
        email = self.normalize_email(email)
        GlobalUserModel = apps.get_model(self.model._meta.app_label, self.model._meta.object_name)
        username = GlobalUserModel.normalize_username(username)
        user = self.model(username=username, username_jp=username_jp, email=email, **extra_fields)
        user.password = make_password(password)
        user.save(using=self._db)

        return user

    def create_user(self, username, username_jp, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_active", False)
        with transaction.atomic():
            user = self._create_user(username, username_jp, email, password, **extra_fields)
            user_activate_token = UserActivateToken.objects.create(user=user)

        subject = "Please Activate Your Account"
        message = (
            f"URLにアクセスしてアカウントを有効化してください。\n "
            f"{settings.DOMAIN_URL}/activate/{user_activate_token.token}/"
        )
        from_email = settings.DEFAULT_FROM_EMAIL
        recipient_list = [
            user.email,
        ]
        send_mail(subject, message, from_email, recipient_list)

        return user

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")

        return self._create_user(username, username, email, password, **extra_fields)

    def change_email(self, user=None, email=""):
        if not user:
            raise ValueError("user_id is not found......")
        if not email:
            raise ValueError("email is not found......")
        user.email = email
        user.is_active = False
        user_activate_token = UserActivateToken.objects.create(user=user)
        subject = "Please Activate Your Changed Email"
        message = (
            f"メールアドレスが変更されたので、URLにアクセスしてアカウントを有効化してください。\n"
            f" {settings.DOMAIN_URL}/activate/{user_activate_token.token}/"
        )
        from_email = settings.DEFAULT_FROM_EMAIL
        recipient_list = [
            email,
        ]
        send_mail(subject, message, from_email, recipient_list)

        user.save()

    def update_user(self, user=None, name="", name_jp="", display_name=""):
        if not user:
            raise ValueError("user_id is not found......")
        if not name:
            raise ValueError("name is not found......")
        if not name_jp:
            raise ValueError("name_jp is not found......")
        if not display_name:
            raise ValueError("display_name is not found......")
        user.name = name
        user.name_jp = name_jp
        user.display_name = display_name
        user.save()


class User(AbstractBaseUser):
    username_validator = UnicodeUsernameValidator()

    created_at = models.DateTimeField("作成日", default=timezone.now)
    updated_at = models.DateTimeField("更新日", default=timezone.now)
    display_name = models.CharField("display_name", max_length=150, default="", blank=True)
    username = models.CharField("username", max_length=150, validators=[username_validator], unique=True)
    username_jp = models.CharField("username(japanese)", max_length=150, validators=[username_validator], unique=True)
    email = models.EmailField("email", unique=True)
    is_staff = models.BooleanField("管理者ステータス", default=False)
    is_active = models.BooleanField("有効", default=False)
    allow_group_add = models.BooleanField("グループ追加許可", default=True)

    # stripe
    stripe_donate_customer_id = models.CharField("Stripe Donate(CusID)", max_length=200, blank=True, null=True)

    groups = models.ManyToManyField(
        "Group",
        blank=True,
        through="UserGroup",
        through_fields=("user", "group"),
        related_name="user_set",
    )
    history = HistoricalRecords()

    objects = UserManager()

    EMAIL_FIELD = "email"
    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        verbose_name = "ユーザ"
        verbose_name_plural = "ユーザ"

    def __str__(self):
        return "%s: %s" % (self.id, self.username)

    def clean(self):
        super().clean()
        self.email = self.__class__.objects.normalize_email(self.email)

    def email_user(self, subject, message, from_email=None, **kwargs):
        """Send an email to this user."""
        send_mail(subject, message, from_email, [self.email], **kwargs)

    def get_display_name(self):
        if self.display_name == "":
            return self.username
        return self.display_name

    def get_full_name(self):
        return self.username

    def get_short_name(self):
        return self.username

    def has_perm(self, perm, obj=None):
        if self.is_active and self.is_staff:
            return True

        return False

    def has_module_perms(self, app_label):
        if self.is_active and self.is_staff:
            return True

        return False


def user_activate_expire_date():
    return timezone.now() + timezone.timedelta(hours=settings.USER_LOGIN_VERIFY_EMAIL_EXPIRED_HOURS)


def email_verify_expire_date():
    return timezone.now() + timezone.timedelta(hours=settings.USER_LOGIN_VERIFY_EMAIL_EXPIRED_MINUTES)


class UserActivateTokensManager(models.Manager):
    def activate_user_by_token(self, activate_token):
        if not self.filter(token=activate_token, expired_at__gt=timezone.now()).exists():
            raise ValueError("Token is not found......")
        user_activate_token = self.filter(token=activate_token, expired_at__gt=timezone.now()).first()
        if user_activate_token.is_used:
            raise ValueError("this token was used...")
        if user_activate_token.user.is_active:
            raise ValueError("アカウントはすでに有効済みです")
        if hasattr(user_activate_token, "user"):
            user = user_activate_token.user
            user.is_active = True
            user.save()
            user_activate_token.is_used = True
            user_activate_token.save()


class UserActivateToken(models.Model):
    created_at = models.DateTimeField("作成日", default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    token = models.UUIDField("Active token", default=uuid.uuid4)
    expired_at = models.DateTimeField("有効期限", default=user_activate_expire_date)
    is_used = models.BooleanField("使用済み", default=False)

    objects = UserActivateTokensManager()

    class Meta:
        verbose_name = "Activate用のToken"
        verbose_name_plural = "Activate用のToken"

    def __str__(self):
        return "%d[%s]: %s" % (self.id, self.user.username, self.token)


def sign_up_key_expire_date():
    return timezone.now() + timezone.timedelta(days=settings.SIGN_UP_EXPIRED_DAYS)


class UserGroup(models.Model):
    created_at = models.DateTimeField("作成日", default=timezone.now)
    updated_at = models.DateTimeField("更新日", default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    is_admin = models.BooleanField("管理者", default=False)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "ユーザ・グループ"
        verbose_name_plural = "ユーザ・グループ"
        unique_together = ("user", "group")

    def __str__(self):
        return "%s-%s" % (self.user.username, self.group.name)


class UserEmailVerifyManager(models.Manager):
    def create_token(self, user=None):
        if not user:
            raise ValueError("user_id is not found......")
        code = random_string(10)
        self.create(user_id=user.id, token=code)
        subject = "認証コード"
        message = render_to_string(
            "mail/account/two_auth.txt",
            {"code": code, "expired_time": settings.USER_LOGIN_VERIFY_EMAIL_EXPIRED_MINUTES},
        )
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)

    def check_token(self, user_id=None, token=""):
        if not user_id:
            raise ValueError("user_id is not found......")
        try:
            sign_up_key = self.filter(token=token, expired_at__gt=timezone.now(), is_used=False).first()
        except Exception:
            return False
        # keyがない時
        if not sign_up_key:
            return False
        sign_up_key.delete()
        return True


class UserEmailVerify(models.Model):
    created_at = models.DateTimeField("作成日", default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    token = models.CharField("token", max_length=100)
    expired_at = models.DateTimeField("有効期限", default=email_verify_expire_date)
    is_used = models.BooleanField("使用済み", default=False)

    objects = UserEmailVerifyManager()

    class Meta:
        verbose_name = "E-Mail用のVerify"
        verbose_name_plural = "E-Mail用のVerify"

    def __str__(self):
        return "%d[%s]" % (
            self.id,
            self.user.username,
        )


class TOTPDeviceManager(models.Manager):
    def check_max_totp_device(self, user=None):
        if not user:
            raise ValueError("user_id is not found......")
        return self.filter(user=user).count() < 5

    def generate_secret(self, email=""):
        otp_secret = pyotp.random_base32()
        return {
            "secret": otp_secret,
            "url": pyotp.totp.TOTP(otp_secret).provisioning_uri(name=email, issuer_name=settings.APP_NAME),
        }

    def create_secret(self, user=None, title="", otp_secret=""):
        if not user:
            raise ValueError("user_id is not found......")
        # 二重送信などで同じシークレットのデバイスが複数できないようにする。
        # (user, secret) の一意制約があるので、並行して送られても get_or_create が競合を吸収する
        self.get_or_create(user=user, secret=otp_secret, defaults={"title": title, "is_active": True})

    def active(self, user):
        """ログインの認証に使えるデバイス（管理画面で無効化したものは除く）"""
        return self.filter(user=user, is_active=True)

    def check_totp(self, user=None, code=""):
        """有効なデバイスのいずれかでコードが一致すれば True

        一度受け付けたコードは同じ時間枠の間も再利用させない（盗み見たコードでの再ログイン対策）。
        使用済みの時間枠は条件付き UPDATE で記録するので、同時に送られても受け付けるのは 1 回だけ。
        """
        if not user:
            raise ValueError("user_id is not found......")
        now = timezone.now()
        for device in self.active(user):
            totp = pyotp.TOTP(device.secret)
            if not totp.verify(code, for_time=now):
                continue
            step = totp.timecode(now)
            # 同じシークレットのデバイスが複数あっても、1 つの UPDATE でまとめて使用済みにする
            # （1 件だけ記録すると、残りのデバイス経由で同じコードを再利用できてしまう）
            consumed = (
                self.filter(user=user, secret=device.secret)
                .filter(models.Q(last_used_step__isnull=True) | models.Q(last_used_step__lt=step))
                .update(last_used_step=step)
            )
            if consumed:
                return True
        return False

    def list(self, user=None):
        if not user:
            raise ValueError("user_id is not found......")
        return self.filter(user=user)

    def remove(self, user=None, id=None):
        if not user:
            raise ValueError("user_id is not found......")
        self.filter(user=user, id=id).delete()


class TOTPDevice(models.Model):
    created_at = models.DateTimeField("作成日", default=timezone.now)
    title = models.CharField("title", max_length=100)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    secret = models.CharField("secret", max_length=100)
    is_active = models.BooleanField("有効", default=False)
    # 最後にログインで受け付けたコードの時間枠（30 秒単位）。同じコードの再利用を防ぐ
    last_used_step = models.BigIntegerField("最終使用の時間枠", blank=True, null=True)

    objects = TOTPDeviceManager()

    class Meta:
        verbose_name = "TOTP Device"
        verbose_name_plural = "TOTP Device"
        constraints = [
            models.UniqueConstraint(fields=["user", "secret"], name="totpdevice_unique_user_secret"),
        ]

    def __str__(self):
        return "%d[%s]" % (self.id, self.user.username)


class TwoFactorChallenge(models.Model):
    """ログイン時の 2 要素認証の確認待ち

    失敗回数をセッションではなく DB で数え、並列リクエストでも試行上限を超えさせない。
    """

    created_at = models.DateTimeField("作成日", default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    attempts = models.PositiveIntegerField("認証コードの試行回数", default=0)

    class Meta:
        verbose_name = "2要素認証の確認待ち"
        verbose_name_plural = "2要素認証の確認待ち"

    def __str__(self):
        return "%d[%s]" % (self.id, self.user.username)


class TwoFactorThrottle(models.Model):
    """ユーザ単位の認証コードの試行制限

    確認待ち（``TwoFactorChallenge``）ごとの上限だけだと、パスワードを知っている攻撃者が
    ログインし直したり並列にセッションを作ったりして試行枠を補充できる。ユーザごとに
    一定時間内の失敗回数を数え、ログインし直してもリセットされない上限をかける。
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    failures = models.PositiveIntegerField("時間枠内の失敗回数", default=0)
    window_started_at = models.DateTimeField("時間枠の開始", default=timezone.now)

    class Meta:
        verbose_name = "2要素認証の試行制限"
        verbose_name_plural = "2要素認証の試行制限"

    def __str__(self):
        return "%s: %d" % (self.user.username, self.failures)


class LineAccount(models.Model):
    """ユーザと LINE アカウントの連携

    LINE ログインは「連携済みのアカウントでログインする」ためだけに使う。
    連携されていない LINE アカウントからは新規ユーザを作らない（ユーザの
    作成はグループ管理者が行う運用のため）。
    """

    created_at = models.DateTimeField("作成日", default=timezone.now)
    updated_at = models.DateTimeField("更新日", default=timezone.now)
    user = models.OneToOneField(User, on_delete=models.CASCADE, verbose_name="ユーザ")
    # LINE の ID トークンの sub。チャネルごとに払い出される一意な ID
    line_user_id = models.CharField("LINEユーザID", max_length=100, unique=True)
    display_name = models.CharField("LINE表示名", max_length=150, default="", blank=True)
    picture_url = models.URLField("アイコンURL", max_length=500, default="", blank=True)
    last_login_at = models.DateTimeField("LINEでの最終ログイン", blank=True, null=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = "LINE連携"
        verbose_name_plural = "LINE連携"

    def __str__(self):
        return f"{self.user.username}: {self.display_name or self.line_user_id}"
