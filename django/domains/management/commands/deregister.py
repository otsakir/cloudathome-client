from django.core.management.base import BaseCommand, CommandError

from cloudlink.services import CloudServerClient, CloudServerError
from domains.services import TunnelConnectionService


class Command(BaseCommand):
    help = 'Tear down this profile\'s connection to its cloud server: disconnect tunnels, release the home slot, and revoke the API token'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help="Don't abort if the cloud server is unreachable or returns an error while "
                 'releasing the home slot or revoking the token -- warn and keep going instead, '
                 'so local teardown (tunnels, cah.py remove) can still complete.',
        )

    def handle(self, *args, **options):
        force = options['force']
        had_errors = False

        TunnelConnectionService.disconnect_all()
        self.stdout.write('Disconnected all tunnels')

        client = CloudServerClient()

        try:
            client.delete_home()
            self.stdout.write('Released home slot')
        except CloudServerError as e:
            if not force:
                raise CommandError(f'Failed to release home slot: {e}')
            had_errors = True
            self.stdout.write(self.style.WARNING(f'Could not release home slot, continuing (--force): {e}'))

        try:
            client.revoke_token()
        except CloudServerError as e:
            if not force:
                raise CommandError(f'Failed to revoke API token: {e}')
            had_errors = True
            self.stdout.write(self.style.WARNING(f'Could not revoke API token, continuing (--force): {e}'))

        if had_errors:
            self.stdout.write(self.style.WARNING(
                'Deregistered locally, but the cloud server did not confirm every step above -- '
                'its records for this home may still be around.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('Deregistered from cloud server'))
