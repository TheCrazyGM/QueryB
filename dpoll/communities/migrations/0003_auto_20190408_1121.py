# Generated by Django 2.1.1 on 2019-04-08 11:21

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('communities', '0002_community_members'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='community',
            options={'verbose_name_plural': 'Communities'},
        ),
        migrations.RemoveField(
            model_name='community',
            name='owners',
        ),
    ]