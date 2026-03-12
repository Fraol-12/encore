# Generated manually to align Playlist.sync_mode default with current model
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("playlists", "0004_alter_syncoperation_options_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="playlist",
            name="sync_mode",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("append_only", "Append Only (add missing, never remove)"),
                    ("smart_diff", "Smart Diff (add/remove based on source, preserve manual changes)"),
                    ("full_replace", "Full Replace (mirror source exactly – destructive)"),
                ],
                default="append_only",
                help_text="How to handle re-syncs",
            ),
        ),
    ]
