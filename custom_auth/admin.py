from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from custom_auth.models import Group, TOTPDevice, User, UserActivateToken


class TermInlineUserAdmin(admin.TabularInline):
    model = User.groups.through
    extra = 0


class TermInlineGroupAdmin(admin.TabularInline):
    model = Group.users.through
    extra = 0


@admin.register(User)
class User(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("username", "username_jp", "password")}),
        ("Personal info", {"fields": ("email",)}),
        ("Flags", {"fields": ("is_active", "is_staff", "allow_group_add")}),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    list_display = (
        "username",
        "username_jp",
        "is_active",
        "is_staff",
    )
    list_filter = (
        "is_staff",
        "is_active",
    )
    search_fields = ("username", "username_jp", "email")
    readonly_fields = (
        "last_login",
        "created_at",
        "updated_at",
    )

    inlines = (TermInlineUserAdmin,)

    def get_groups(self, obj):
        return "\n".join([p.groups for p in obj.group.all()])


@admin.register(Group)
class Group(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": (
            "name", "name_jp", "status", "comment")}),
        ("Membership", {"fields": ("membership_type", "membership_expired_at")}),
        (
            "無償化",
            {
                "description": (
                    "運営判断で Stripe 契約なしに有料プラン相当を付与する。"
                    "期限は Membership の「有効期限」を使う（未設定なら無期限）。"
                    "有料契約と併用している場合は上位のプランが適用される。"
                ),
                "fields": ("free_plan", "free_reason"),
            },
        ),
        (
            "Stripe",
            {
                "description": "Stripe 側の状態のキャッシュ。Webhook と請求画面で自動的に同期される。",
                "classes": ("collapse",),
                "fields": (
                    "stripe_customer_id",
                    "stripe_subscription_id",
                    "stripe_plan",
                    "stripe_status",
                    "stripe_current_period_end",
                    "stripe_cancel_at_period_end",
                ),
            },
        ),
        (
            "Personal info",
            {
                "fields": (
                    "postcode",
                    "address",
                    "address_jp",
                    "phone",
                    "country",
                    "contract_type",
                )
            },
        ),
    )
    list_display = (
        "name",
        "name_jp",
        "membership_type",
        "membership_expired_at",
        "free_plan",
        "stripe_plan",
        "stripe_status",
    )
    list_filter = (
        "membership_type",
        "free_plan",
        "stripe_status",
    )
    search_fields = (
        "name",
        "name_jp",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        # Stripe が真実の情報源なので管理画面からは編集させない
        "stripe_customer_id",
        "stripe_subscription_id",
        "stripe_plan",
        "stripe_status",
        "stripe_current_period_end",
        "stripe_cancel_at_period_end",
    )

    inlines = (
        TermInlineGroupAdmin,
    )


@admin.register(UserActivateToken)
class UserActivateToken(admin.ModelAdmin):
    fieldsets = (
        (None, {"fields": ("user", "token", "expired_at", "is_used")}),
        ("Important dates", {"fields": ("created_at",)}),
    )
    list_display = ("user", "token", "expired_at", "is_used")
    list_filter = ("is_used",)
    search_fields = ("token", "expired_at", "is_used")


@admin.register(TOTPDevice)
class TOTPDevice(admin.ModelAdmin):
    fieldsets = (
        (None, {"fields": ("title", "is_active", "user", "secret")}),
        ("Important dates", {"fields": ("created_at",)}),
    )
    list_display = (
        "id",
        "is_active",
        "title",
        "user",
    )
    list_filter = ("is_active",)
    search_fields = (
        "id",
        "is_active",
        "title",
    )
