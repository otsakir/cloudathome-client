from django.apps import AppConfig


class DomainsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'domains'

    def ready(self):
        from domains.supervisor import start_supervisor_if_enabled
        start_supervisor_if_enabled()

