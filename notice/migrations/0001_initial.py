# Generated by Django 5.1.4 on 2025-01-14 21:22

import django.db.models.deletion
import django.utils.timezone
import shifttimes.models
import simple_history.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Notice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='作成日')),
                ('start_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='通知開始日')),
                ('end_at', models.DateTimeField(blank=True, null=True, verbose_name='通知終了日')),
                ('is_active', models.BooleanField(default=True, verbose_name='有効')),
                ('type1', models.CharField(choices=[('サービス情報', 'サービス情報'), ('その他', 'その他')], max_length=200, verbose_name='type1')),
                ('title', models.CharField(max_length=250, verbose_name='title')),
                ('body', shifttimes.models.MediumTextField(blank=True, default='', verbose_name='内容')),
                ('is_important', models.BooleanField(default=False, verbose_name='重要')),
                ('is_fail', models.BooleanField(default=False, verbose_name='障害')),
                ('is_info', models.BooleanField(default=False, verbose_name='情報')),
            ],
            options={
                'ordering': ('-end_at',),
            },
        ),
        migrations.CreateModel(
            name='HistoricalNotice',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='作成日')),
                ('start_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='通知開始日')),
                ('end_at', models.DateTimeField(blank=True, null=True, verbose_name='通知終了日')),
                ('is_active', models.BooleanField(default=True, verbose_name='有効')),
                ('type1', models.CharField(choices=[('サービス情報', 'サービス情報'), ('その他', 'その他')], max_length=200, verbose_name='type1')),
                ('title', models.CharField(max_length=250, verbose_name='title')),
                ('body', shifttimes.models.MediumTextField(blank=True, default='', verbose_name='内容')),
                ('is_important', models.BooleanField(default=False, verbose_name='重要')),
                ('is_fail', models.BooleanField(default=False, verbose_name='障害')),
                ('is_info', models.BooleanField(default=False, verbose_name='情報')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'historical notice',
                'verbose_name_plural': 'historical notices',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
    ]
