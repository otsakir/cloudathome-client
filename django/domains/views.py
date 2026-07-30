import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import ListView, DetailView, FormView

from cloudlink.services import CloudServerClient, CloudServerError
from domains.forms import AddDomainForm, IssueCertificateForm, ProxyEntryForm, TcpProxyEntryForm
from domains.models import Domain, ProxyEntry
from domains.services import CertbotError, CertbotService, SyncService, TunnelService

logger = logging.getLogger(__name__)


def _delete_proxy_entry(entry):
    """Close the tunnel, remove the cloud mapping, and delete the local record."""
    if entry.tunnel_pid:
        TunnelService.close_tunnel(entry.tunnel_pid)
    client = CloudServerClient()
    try:
        if entry.scheme == ProxyEntry.SCHEME_TCP:
            client.delete_proxy_mapping('tcp', public_port=entry.public_port)
        else:
            client.delete_proxy_mapping(entry.scheme, host=entry.domain.name)
    except CloudServerError as e:
        logger.info('_delete_proxy_entry %r: no cloud mapping to remove (%s)', entry, e)
    entry.delete()


class DomainListView(ListView):
    model = Domain
    template_name = 'domains/domain_list.html'
    context_object_name = 'domains'

    def get_queryset(self):
        return Domain.objects.prefetch_related('proxy_entries').all()

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs)


class AddDomainView(FormView):
    template_name = 'domains/add_domain.html'
    form_class = AddDomainForm

    def get_base_domains(self):
        """None means "couldn't check" (fail open); [] means "checked, none registered"."""
        if not hasattr(self, '_base_domains'):
            try:
                self._base_domains = [d['domain'] for d in CloudServerClient().list_base_domains()]
            except Exception:
                self._base_domains = None
        return self._base_domains

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['base_domains'] = self.get_base_domains()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['base_domains'] = self.get_base_domains()
        return context

    def form_valid(self, form):
        domain, _ = Domain.objects.get_or_create(name=form.cleaned_data['name'])
        return redirect('domain_detail', pk=domain.pk)


class DomainDetailView(DetailView):
    model = Domain
    template_name = 'domains/domain_detail.html'


class DeleteDomainView(View):
    def get(self, request, pk):
        domain = get_object_or_404(Domain, pk=pk)
        return render(request, 'domains/delete_domain.html', {'domain': domain})

    def post(self, request, pk):
        domain = get_object_or_404(Domain, pk=pk)
        for entry in list(domain.proxy_entries.all()):
            try:
                _delete_proxy_entry(entry)
            except Exception:
                logger.exception('Failed to clean up proxy entry %r while deleting domain %r', entry, domain)
        domain.delete()
        return redirect('domain_list')


class ProxyEntryCreateView(FormView):
    template_name = 'domains/add_proxy_entry.html'
    form_class = ProxyEntryForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.domain = get_object_or_404(Domain, pk=kwargs['domain_pk'])

    def get_context_data(self, **kwargs):
        from cloudlink.config import get_config
        context = super().get_context_data(**kwargs)
        cfg = get_config()
        context['domain'] = self.domain
        context['lan_forwarding'] = cfg.features.lan_forwarding
        context['http_port_base'] = cfg.http_port_base
        context['https_port_base'] = cfg.https_port_base
        if cfg.http_port_base is not None and cfg.http_port_count is not None:
            context['http_port_max'] = cfg.http_port_base + cfg.http_port_count - 1
        if cfg.https_port_base is not None and cfg.https_port_count is not None:
            context['https_port_max'] = cfg.https_port_base + cfg.https_port_count - 1
        return context

    def form_valid(self, form):
        from cloudlink.config import get_config
        cfg = get_config()
        if cfg.features.lan_forwarding:
            home_host = form.cleaned_data.get('home_host') or 'localhost'
        else:
            home_host = 'localhost'
        home_port = form.cleaned_data['home_port']
        scheme = form.cleaned_data['scheme']
        public_port = form.cleaned_data.get('public_port')

        if ProxyEntry.objects.filter(home_host=home_host, home_port=home_port).exists():
            form.add_error(None, f'{home_host}:{home_port} is already used by another proxy entry.')
            return self.form_invalid(form)

        if self.domain.proxy_entries.filter(scheme=scheme).exists():
            form.add_error(None, f'This domain already has a {scheme.upper()} proxy entry.')
            return self.form_invalid(form)

        if public_port is not None:
            default_port = 80 if scheme == ProxyEntry.SCHEME_HTTP else 443
            base, count = (
                (cfg.http_port_base, cfg.http_port_count) if scheme == ProxyEntry.SCHEME_HTTP
                else (cfg.https_port_base, cfg.https_port_count)
            )
            in_range = base is not None and count is not None and base <= public_port < base + count
            if public_port != default_port and not in_range:
                if base is not None and count is not None:
                    form.add_error('public_port', f'Must be {default_port} (default) or in range {base}–{base + count - 1}.')
                else:
                    form.add_error('public_port', f'Must be {default_port} (default).')
                return self.form_invalid(form)

        client = CloudServerClient()
        try:
            result = client.create_proxy_mapping(scheme, host=self.domain.name, public_port=public_port)
        except CloudServerError as e:
            form.add_error(None, str(e))
            return self.form_invalid(form)

        entry = ProxyEntry.objects.create(
            domain=self.domain,
            tunnel_port=result['tunnel_port'],
            public_port=result.get('public_port'),
            home_host=home_host,
            home_port=home_port,
            scheme=scheme,
        )
        return redirect('proxy_entry_detail', pk=entry.pk)


class ProxyEntryDetailView(DetailView):
    model = ProxyEntry
    template_name = 'domains/proxy_entry_detail.html'

    def get_object(self, queryset=None):
        entry = super().get_object(queryset)
        # Correct stale "open" status if the SSH process is no longer running
        if entry.tunnel_status == ProxyEntry.TUNNEL_OPEN and entry.tunnel_pid:
            if not TunnelService.is_running(entry.tunnel_pid):
                entry.tunnel_pid = None
                entry.tunnel_status = ProxyEntry.TUNNEL_CLOSED
                entry.save()
        return entry

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entry = self.object
        context['home_port_open'] = TunnelService.is_home_port_open(entry.home_host, entry.home_port)
        return context


class IssueCertificateView(FormView):
    template_name = 'domains/issue_certificate.html'
    form_class = IssueCertificateForm

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.entry = get_object_or_404(ProxyEntry, pk=kwargs['pk'])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['entry'] = self.entry
        return context

    def form_valid(self, form):
        if self.entry.scheme == ProxyEntry.SCHEME_HTTPS:
            form.add_error(
                None,
                "Can't issue a certificate from an HTTPS proxy entry -- the ACME HTTP-01 "
                'challenge needs a plain-HTTP route, which only an HTTP proxy entry for '
                'this domain provides.',
            )
            return self.form_invalid(form)
        try:
            CertbotService.obtain_certificate(
                self.entry.domain,
                form.cleaned_data['email'],
                self.entry.home_port,
            )
        except CertbotError as e:
            form.add_error(None, str(e))
            return self.form_invalid(form)
        return redirect('proxy_entry_detail', pk=self.entry.pk)


class TcpProxyEntryCreateView(FormView):
    template_name = 'domains/add_tcp_proxy_entry.html'
    form_class = TcpProxyEntryForm

    def get_context_data(self, **kwargs):
        from cloudlink.config import get_config
        context = super().get_context_data(**kwargs)
        cfg = get_config()
        context['tcp_port_base'] = cfg.tcp_port_base
        context['tcp_port_count'] = cfg.tcp_port_count
        if cfg.tcp_port_base is not None and cfg.tcp_port_count is not None:
            context['tcp_port_max'] = cfg.tcp_port_base + cfg.tcp_port_count - 1
        context['lan_forwarding'] = cfg.features.lan_forwarding
        return context

    def form_valid(self, form):
        from cloudlink.config import get_config
        cfg = get_config()
        public_port = form.cleaned_data['public_port']
        home_port = form.cleaned_data['home_port']
        home_host = form.cleaned_data.get('home_host') or 'localhost'
        if not cfg.features.lan_forwarding:
            home_host = 'localhost'

        if cfg.tcp_port_base is not None and cfg.tcp_port_count is not None:
            if not (cfg.tcp_port_base <= public_port < cfg.tcp_port_base + cfg.tcp_port_count):
                form.add_error('public_port', f'Must be in range {cfg.tcp_port_base}–{cfg.tcp_port_base + cfg.tcp_port_count - 1}.')
                return self.form_invalid(form)

        if ProxyEntry.objects.filter(public_port=public_port).exists():
            form.add_error('public_port', 'This public port is already registered.')
            return self.form_invalid(form)

        if ProxyEntry.objects.filter(home_host=home_host, home_port=home_port).exists():
            form.add_error(None, f'{home_host}:{home_port} is already used by another proxy entry.')
            return self.form_invalid(form)

        client = CloudServerClient()
        try:
            result = client.create_proxy_mapping('tcp', public_port=public_port)
        except CloudServerError as e:
            form.add_error(None, str(e))
            return self.form_invalid(form)

        entry = ProxyEntry.objects.create(
            scheme=ProxyEntry.SCHEME_TCP,
            public_port=public_port,
            tunnel_port=result['tunnel_port'],
            home_host=home_host,
            home_port=home_port,
        )
        return redirect('proxy_entry_detail', pk=entry.pk)


class DeleteProxyEntryView(View):
    def post(self, request, pk):
        entry = get_object_or_404(ProxyEntry, pk=pk)
        domain_pk = entry.domain_id
        _delete_proxy_entry(entry)
        if domain_pk:
            return redirect('domain_detail', pk=domain_pk)
        return redirect('domain_list')


class SyncAllView(View):
    def post(self, request):
        succeeded, failed = SyncService.sync_all()
        if failed:
            messages.warning(request, f'Connect all: {succeeded} connected, {failed} failed')
        else:
            messages.success(request, f'All tunnels connected ({succeeded} entries)')
        return redirect('dashboard')


class DisconnectAllView(View):
    def post(self, request):
        SyncService.disconnect_all()
        messages.success(request, 'All tunnels disconnected')
        return redirect('dashboard')


class SyncEntryView(View):
    def post(self, request, pk):
        entry = get_object_or_404(ProxyEntry, pk=pk)
        try:
            SyncService.sync_entry(entry)
            messages.success(request, 'Entry synced successfully')
        except Exception as e:
            logger.exception('Sync failed for proxy entry %r', entry)
            messages.error(request, f'Sync failed: {e}')
        return redirect('proxy_entry_detail', pk=entry.pk)


class TunnelToggleView(View):
    def post(self, request, pk):
        entry = get_object_or_404(ProxyEntry, pk=pk)
        if entry.tunnel_status == ProxyEntry.TUNNEL_OPEN:
            if entry.tunnel_pid:
                TunnelService.close_tunnel(entry.tunnel_pid)
            entry.tunnel_pid = None
            entry.tunnel_status = ProxyEntry.TUNNEL_CLOSED
        else:
            try:
                pid = TunnelService.open_tunnel(entry.tunnel_port, entry.home_port, entry.home_host)
                entry.tunnel_pid = pid
                entry.tunnel_status = ProxyEntry.TUNNEL_OPEN
            except Exception as e:
                logger.exception('Failed to open tunnel for proxy entry %r', entry)
                entry.tunnel_status = ProxyEntry.TUNNEL_ERROR
                messages.error(request, f'Failed to open tunnel: {e}')
        entry.save()
        return redirect('proxy_entry_detail', pk=entry.pk)
