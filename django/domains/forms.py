from django import forms

from domains.models import ProxyEntry


class AddDomainForm(forms.Form):
    name = forms.CharField(
        max_length=253,
        label='Domain name',
        help_text='e.g. mysite.example.com',
    )

    def __init__(self, *args, base_domains=None, **kwargs):
        """base_domains: list of registered base domains, or None if they
        couldn't be fetched (in which case validation is skipped -- the
        cloud will still enforce this when a proxy mapping is created)."""
        super().__init__(*args, **kwargs)
        self.base_domains = base_domains

    def clean_name(self):
        name = self.cleaned_data['name'].strip().lower()
        if self.base_domains is not None:
            if not self.base_domains:
                raise forms.ValidationError(
                    'You have no registered base domains yet. Register one from the dashboard first.'
                )
            if not any(name == bd or name.endswith('.' + bd) for bd in self.base_domains):
                raise forms.ValidationError(
                    f"'{name}' must be equal to or a subdomain of one of your registered base "
                    f"domains: {', '.join(self.base_domains)}"
                )
        return name


class ProxyEntryForm(forms.Form):
    scheme = forms.ChoiceField(
        choices=[(ProxyEntry.SCHEME_HTTP, 'HTTP'), (ProxyEntry.SCHEME_HTTPS, 'HTTPS')],
        label='Scheme',
    )
    public_port = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=65535,
        label='Public port',
        help_text='Leave blank for the cloud\'s default port, or pick one from the ranges below',
    )
    home_host = forms.CharField(
        max_length=253,
        initial='localhost',
        required=False,
        label='Home network host',
        help_text='Hostname or IP of the target service on the home network.',
    )
    home_port = forms.IntegerField(
        min_value=1,
        max_value=65535,
        label='Home port',
        help_text='Port of the local service',
    )

    def __init__(self, *args, inbound_ports=None, **kwargs):
        """inbound_ports: {scheme: (default_port, range_base, range_count)} as cached
        from the cloud (see CloudConfig.inbound_ports). The cloud re-validates the
        port regardless, since the cached values can be stale."""
        super().__init__(*args, **kwargs)
        self.inbound_ports = inbound_ports or {}

    def clean_public_port(self):
        public_port = self.cleaned_data.get('public_port')
        scheme = self.cleaned_data.get('scheme')
        if public_port is None or scheme not in self.inbound_ports:
            return public_port
        default, base, count = self.inbound_ports[scheme]
        if public_port == default:
            return public_port
        if base is None or count is None:
            raise forms.ValidationError(f'Must be {default} (default).')
        if not base <= public_port < base + count:
            raise forms.ValidationError(f'Must be {default} (default) or in range {base}–{base + count - 1}.')
        return public_port


class TcpProxyEntryForm(forms.Form):
    public_port = forms.IntegerField(
        min_value=1,
        max_value=65535,
        label='Public port',
        help_text='Port on the cloud server clients will connect to (must be within your allocated TCP range).',
    )
    home_host = forms.CharField(
        max_length=253,
        initial='localhost',
        required=False,
        label='Home network host',
        help_text='Hostname or IP of the target service on the home network.',
    )
    home_port = forms.IntegerField(
        min_value=1,
        max_value=65535,
        label='Home port',
        help_text='Port of the local service to expose.',
    )


class IssueCertificateForm(forms.Form):
    email = forms.EmailField(
        label='Email address',
        help_text="Used by Let's Encrypt for renewal notifications.",
    )
