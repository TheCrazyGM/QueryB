# Generated by Django 2.1.1 on 2018-11-26 19:42

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sponsors', '0002_sponsor_opt_in_to_rewards'),
    ]

    operations = [
        migrations.AddField(
            model_name='sponsor',
            name='delegation_created_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='sponsor',
            name='delegation_modified_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
