from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('domains', '0012_proxyentry_desired_up'),
    ]

    operations = [
        migrations.RenameField(
            model_name='proxyentry',
            old_name='desired_up',
            new_name='keep_open',
        ),
    ]
