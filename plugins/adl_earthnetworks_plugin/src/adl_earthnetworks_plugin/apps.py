from adl.core.registries import plugin_registry
from django.apps import AppConfig


class EarthNetworksConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = "adl_earthnetworks_plugin"

    def ready(self):
        from .plugins import EarthNetworksPlugin

        plugin_registry.register(EarthNetworksPlugin())
