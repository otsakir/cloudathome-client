import logging

from django.core.management.base import BaseCommand, CommandError

from domains.models import Domain
from domains.services import TunnelConnectionService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Reconnect tunnels and cloud proxy mappings'

    def add_arguments(self, parser):
        parser.add_argument(
            '--domain',
            metavar='NAME',
            help='Reconnect only the entry for this domain name',
        )
        parser.add_argument(
            '--disconnect',
            action='store_true',
            help='Disconnect instead of reconnect',
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
                    TunnelConnectionService.disconnect_entry(entry)
                self.stdout.write(self.style.SUCCESS(
                    f'Disconnected {domain_name} ({", ".join(e.scheme.upper() for e in entries)})'
                ))
            else:
                failed_schemes = []
                for entry in entries:
                    try:
                        TunnelConnectionService.reconnect_entry(entry)
                    except Exception:
                        logger.exception('reconnect_tunnels --domain %s: reconnect failed for scheme %s', domain_name, entry.scheme)
                        failed_schemes.append(entry.scheme.upper())
                if failed_schemes:
                    raise CommandError(f'Reconnect failed for {domain_name}: {", ".join(failed_schemes)}')
                self.stdout.write(self.style.SUCCESS(
                    f'Reconnected {domain_name} ({", ".join(e.scheme.upper() for e in entries)})'
                ))
        else:
            if disconnect:
                TunnelConnectionService.disconnect_all()
                self.stdout.write(self.style.SUCCESS('Disconnected all entries'))
            else:
                succeeded, failed = TunnelConnectionService.reconnect_all()
                msg = f'Reconnect complete: {succeeded} succeeded, {failed} failed'
                if failed:
                    self.stdout.write(self.style.WARNING(msg))
                else:
                    self.stdout.write(self.style.SUCCESS(msg))
