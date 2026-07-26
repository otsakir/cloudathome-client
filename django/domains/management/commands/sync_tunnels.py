import logging

from django.core.management.base import BaseCommand, CommandError

from domains.models import Domain
from domains.services import SyncService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Sync tunnels and cloud proxy mappings'

    def add_arguments(self, parser):
        parser.add_argument(
            '--domain',
            metavar='NAME',
            help='Sync only the entry for this domain name',
        )
        parser.add_argument(
            '--disconnect',
            action='store_true',
            help='Disconnect instead of sync',
        )

    def handle(self, *args, **options):
        domain_name = options['domain']
        disconnect = options['disconnect']

        if domain_name:
            domain = Domain.objects.filter(name=domain_name).first()
            if not domain:
                raise CommandError(f'Domain not found: {domain_name}')
            entries = list(domain.proxy_entries.all())
            if not entries:
                raise CommandError(f'No proxy entry for domain: {domain_name}')

            if disconnect:
                for entry in entries:
                    SyncService.disconnect_entry(entry)
                self.stdout.write(self.style.SUCCESS(
                    f'Disconnected {domain_name} ({", ".join(e.scheme.upper() for e in entries)})'
                ))
            else:
                failed_schemes = []
                for entry in entries:
                    try:
                        SyncService.sync_entry(entry)
                    except Exception:
                        logger.exception('sync_tunnels --domain %s: sync failed for scheme %s', domain_name, entry.scheme)
                        failed_schemes.append(entry.scheme.upper())
                if failed_schemes:
                    raise CommandError(f'Sync failed for {domain_name}: {", ".join(failed_schemes)}')
                self.stdout.write(self.style.SUCCESS(
                    f'Synced {domain_name} ({", ".join(e.scheme.upper() for e in entries)})'
                ))
        else:
            if disconnect:
                SyncService.disconnect_all()
                self.stdout.write(self.style.SUCCESS('Disconnected all entries'))
            else:
                succeeded, failed = SyncService.sync_all()
                msg = f'Sync complete: {succeeded} succeeded, {failed} failed'
                if failed:
                    self.stdout.write(self.style.WARNING(msg))
                else:
                    self.stdout.write(self.style.SUCCESS(msg))
