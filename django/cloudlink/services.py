import requests

from cloudlink.config import get_config


class CloudServerError(Exception):
    pass


class PublicPortNotOfferedError(CloudServerError):
    """The cloud refused a mapping's public_port as outside what it currently
    offers for the scheme (its `code: public_port_not_offered` 400). For a
    mapping being re-registered, this means the cloud operator changed the
    inbound port range since the mapping was created."""

    def __init__(self, public_port, default_port, ranges):
        self.public_port = public_port
        self.default_port = default_port
        self.ranges = ranges
        offered = [str(default_port)] + [
            f'{r["port_base"]}–{r["port_base"] + r["port_count"] - 1}' for r in ranges
        ]
        super().__init__(
            f'port {public_port} is not offered by the cloud server (available: {", ".join(offered)})'
        )


class CloudServerClient:

    def _headers(self):
        return {'Authorization': f'Token {get_config().auth_token}'}

    def _url(self, path):
        return f'{get_config().cloudserver_url.rstrip("/")}/{path.lstrip("/")}'

    def get_home(self):
        resp = requests.get(self._url('/api/homes/'), headers=self._headers())
        if resp.status_code != 200:
            raise CloudServerError(f'get_home failed: {resp.status_code} {resp.text}')
        homes = resp.json()
        if not homes:
            raise CloudServerError('no homes assigned to this account')
        return homes[0]

    def create_proxy_mapping(self, scheme, host=None, public_port=None):
        slug = get_config().home_slug
        if scheme == 'tcp':
            url = self._url(f'/api/homes/{slug}/proxy-mappings/tcp/')
            payload = {'public_port': public_port}
        else:
            url = self._url(f'/api/homes/{slug}/proxy-mappings/{scheme}/')
            payload = {'host': host, 'scheme': scheme}
            if public_port is not None:
                payload['public_port'] = public_port
        resp = requests.post(url, headers=self._headers(), json=payload)
        if resp.status_code == 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get('code') == 'public_port_not_offered':
                raise PublicPortNotOfferedError(public_port, body['default_port'], body['ranges'])
        if resp.status_code != 201:
            raise CloudServerError(f'create_proxy_mapping failed: {resp.status_code} {resp.text}')
        return resp.json()

    def delete_proxy_mapping(self, scheme, host=None, public_port=None):
        """public_port is required for every scheme: a host may have independent
        HTTP/HTTPS mappings at more than one port (the cloud routes on
        hostname:destination_port, not hostname alone), so it's needed to
        disambiguate which one to remove, same as it always has been for TCP."""
        slug = get_config().home_slug
        if scheme == 'tcp':
            url = self._url(f'/api/homes/{slug}/proxy-mappings/tcp/{public_port}/')
        else:
            url = self._url(f'/api/homes/{slug}/proxy-mappings/{scheme}/{host}/{public_port}/')
        resp = requests.delete(url, headers=self._headers())
        if resp.status_code != 204:
            raise CloudServerError(f'delete_proxy_mapping failed: {resp.status_code} {resp.text}')

    def list_base_domains(self):
        resp = requests.get(
            self._url(f'/api/homes/{get_config().home_slug}/base-domains/'),
            headers=self._headers(),
        )
        if resp.status_code != 200:
            raise CloudServerError(f'list_base_domains failed: {resp.status_code} {resp.text}')
        return resp.json()

    def add_base_domain(self, domain):
        resp = requests.post(
            self._url(f'/api/homes/{get_config().home_slug}/base-domains/'),
            headers=self._headers(),
            json={'domain': domain},
        )
        if resp.status_code == 409:
            raise CloudServerError(resp.json().get('message', 'conflict'))
        if resp.status_code != 201:
            raise CloudServerError(f'add_base_domain failed: {resp.status_code} {resp.text}')
        return resp.json()

    def remove_base_domain(self, domain):
        resp = requests.delete(
            self._url(f'/api/homes/{get_config().home_slug}/base-domains/{domain}/'),
            headers=self._headers(),
        )
        if resp.status_code == 409:
            raise CloudServerError(resp.json().get('message', 'conflict'))
        if resp.status_code != 204:
            raise CloudServerError(f'remove_base_domain failed: {resp.status_code} {resp.text}')

    def update_bandwidth(self, kbps_or_none):
        resp = requests.patch(
            self._url(f'/api/homes/{get_config().home_slug}/'),
            headers=self._headers(),
            json={'bandwidth_limit_kbps': kbps_or_none},
        )
        if resp.status_code != 200:
            raise CloudServerError(f'update_bandwidth failed: {resp.status_code} {resp.text}')
        return resp.json()

    def delete_home(self):
        """Release this home's slot. The cloud also cleans up its base domains, live
        mappings, and bandwidth limit -- see HomeRetrieveDestroyApiView.destroy().
        A 404 means it was already released by a prior partial run; treat as success."""
        resp = requests.delete(
            self._url(f'/api/homes/{get_config().home_slug}/'),
            headers=self._headers(),
        )
        if resp.status_code == 404:
            return
        if resp.status_code != 204:
            raise CloudServerError(f'delete_home failed: {resp.status_code} {resp.text}')

    def revoke_token(self):
        resp = requests.delete(self._url('/api/auth/token/'), headers=self._headers())
        if resp.status_code != 204:
            raise CloudServerError(f'revoke_token failed: {resp.status_code} {resp.text}')
