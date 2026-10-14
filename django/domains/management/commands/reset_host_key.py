from django.core.management.base import BaseCommand

from cloudlink import hostkey


class Command(BaseCommand):
    help = "Forget the cloud server's trusted SSH host key, e.g. after the cloud was rebuilt"

    def add_arguments(self, parser):
        parser.add_argument('-y', '--yes', action='store_true', help='Skip the confirmation prompt')

    def handle(self, *args, **options):
        change = hostkey.get_host_key_change()
        trusted = hostkey.trusted_fingerprints()

        self.stdout.write(f'SSH host: {hostkey.ssh_host_spec()}')
        if trusted:
            for key_type, fp in trusted:
                self.stdout.write(f'  trusted until now: {key_type} {fp}')
        else:
            self.stdout.write('  no trusted host key stored')
        if change:
            self.stdout.write(
                f'  changed key seen: {change.get("fingerprint") or "unknown"} '
                f'(detected {change.get("detected_at")})'
            )
            self.stdout.write(
                'Compare the new fingerprint with the one shown on the cloud dashboard: '
                'unless the cloud server was rebuilt or redeployed, a changed key may '
                'indicate the connection is being intercepted.'
            )

        if not trusted and not change:
            self.stdout.write('Nothing to reset.')
            return

        if not options['yes'] and input('Forget the trusted key? [y/N]: ').strip().lower() != 'y':
            self.stdout.write('Aborted.')
            return

        hostkey.reset_host_key()
        self.stdout.write(self.style.SUCCESS(
            'Host key forgotten. The next tunnel connection will trust the key the cloud presents: '
            'use "Connect all" in the Home Console, or (re)start it with cah.py start.'
        ))
