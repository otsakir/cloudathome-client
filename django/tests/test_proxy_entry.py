from unittest.mock import MagicMock, patch

import pytest
from django.db import IntegrityError, transaction

from domains.models import Domain, ProxyEntry
from playbooks.certificate import IssueCertificatePlaybook


@pytest.mark.django_db
def test_domain_can_have_both_http_and_https_entries():
    domain = Domain.objects.create(name='mysite.example.com')
    ProxyEntry.objects.create(domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8080, tunnel_port=2000)
    ProxyEntry.objects.create(domain=domain, scheme=ProxyEntry.SCHEME_HTTPS, home_port=8443, tunnel_port=2001)

    assert domain.proxy_entries.count() == 2

    with transaction.atomic():
        with pytest.raises(IntegrityError):
            ProxyEntry.objects.create(domain=domain, scheme=ProxyEntry.SCHEME_HTTP, home_port=8081, tunnel_port=2002)


@pytest.mark.django_db
def test_issue_certificate_playbook_succeeds_when_domain_already_has_https_entry():
    domain = Domain.objects.create(name='mysite.example.com')
    ProxyEntry.objects.create(domain=domain, scheme=ProxyEntry.SCHEME_HTTPS, home_port=8443, tunnel_port=2001)

    with patch('playbooks.certificate.CloudServerClient') as MockClient, \
         patch('playbooks.certificate.TunnelService') as MockTunnel, \
         patch('playbooks.certificate.CertbotService') as MockCertbot:
        MockClient.return_value.create_proxy_mapping.return_value = {'tunnel_port': 2000}
        MockTunnel.open_tunnel.return_value = 12345
        MockCertbot.obtain_certificate = MagicMock()

        result = IssueCertificatePlaybook().run(
            domain_name='mysite.example.com', email='a@example.com', home_port=8080,
        )

    assert result.success, [f'{s.name}: {s.detail}' for s in result.steps if s.status == 'error']
    # The pre-existing HTTPS entry must survive untouched; the temporary HTTP
    # entry is created and cleaned up by the playbook.
    remaining = list(domain.proxy_entries.all())
    assert len(remaining) == 1
    assert remaining[0].scheme == ProxyEntry.SCHEME_HTTPS
