from unittest.mock import MagicMock, patch

import pytest

from cloudlink.services import CloudServerClient, CloudServerError, PublicPortNotOfferedError
from domains.models import Domain, ProxyEntry
from domains.services import TunnelConnectionService


def _response(status_code, body):
    resp = MagicMock(status_code=status_code, text=str(body))
    resp.json.return_value = body
    return resp


NOT_OFFERED_BODY = {
    'code': 'public_port_not_offered',
    'message': 'public_port must be 443 (default) or in range 9443–9452',
    'default_port': 443,
    'ranges': [{'port_base': 9443, 'port_count': 10}],
}


def test_create_proxy_mapping_raises_specific_error_for_port_not_offered():
    with patch('cloudlink.services.requests.post', return_value=_response(400, NOT_OFFERED_BODY)):
        with pytest.raises(PublicPortNotOfferedError) as exc_info:
            CloudServerClient().create_proxy_mapping('https', host='mysite.example.com', public_port=8443)

    assert exc_info.value.public_port == 8443
    assert str(exc_info.value) == 'port 8443 is not offered by the cloud server (available: 443, 9443–9452)'


def test_create_proxy_mapping_other_400_stays_generic():
    with patch('cloudlink.services.requests.post', return_value=_response(400, {'host': ['This field is required.']})):
        with pytest.raises(CloudServerError) as exc_info:
            CloudServerClient().create_proxy_mapping('https', host='mysite.example.com')

    assert not isinstance(exc_info.value, PublicPortNotOfferedError)


@pytest.mark.django_db
def test_reconnect_records_port_not_offered_reason_on_entry():
    """After the cloud operator shrinks the inbound range, reconnect_all() runs
    unattended and only logs failures -- the reason must be kept on the entry
    for the UI to show."""
    domain = Domain.objects.create(name='mysite.example.com')
    entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTPS, public_port=8443, home_port=9443, tunnel_port=2000,
    )
    error = PublicPortNotOfferedError(8443, 443, [{'port_base': 9443, 'port_count': 10}])

    with patch('domains.services.CloudServerClient') as MockClient, \
         patch('domains.services.TunnelService') as MockTunnel:
        MockClient.return_value.create_proxy_mapping.side_effect = error
        with pytest.raises(PublicPortNotOfferedError):
            TunnelConnectionService.reconnect_entry(entry)
        MockTunnel.open_tunnel.assert_not_called()

    entry.refresh_from_db()
    assert entry.tunnel_status == ProxyEntry.TUNNEL_ERROR
    assert entry.tunnel_error == str(error)


@pytest.mark.django_db
def test_successful_reconnect_clears_previous_error():
    domain = Domain.objects.create(name='mysite.example.com')
    entry = ProxyEntry.objects.create(
        domain=domain, scheme=ProxyEntry.SCHEME_HTTPS, public_port=9443, home_port=9443, tunnel_port=2000,
        tunnel_status=ProxyEntry.TUNNEL_ERROR, tunnel_error='port 8443 is not offered by the cloud server',
    )

    with patch('domains.services.CloudServerClient') as MockClient, \
         patch('domains.services.TunnelService') as MockTunnel:
        MockClient.return_value.create_proxy_mapping.return_value = {'tunnel_port': 2001, 'public_port': 9443}
        MockTunnel.open_tunnel.return_value = 12345
        TunnelConnectionService.reconnect_entry(entry)

    entry.refresh_from_db()
    assert entry.tunnel_status == ProxyEntry.TUNNEL_OPEN
    assert entry.tunnel_error == ''
