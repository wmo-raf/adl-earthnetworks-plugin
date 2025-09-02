from adl.core.models import NetworkConnection, StationLink, DataParameter, Unit
from django.db import models
from django.utils.translation import gettext_lazy as _
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, InlinePanel
from wagtail.models import Orderable

from .client import EarthNetworksClient
from .validators import validate_start_date


class EarthNetworksConnection(NetworkConnection):
    """
    Model representing a connection to EarthNetworks Data.
    """
    station_link_model_string_label = "adl_earthnetworks_plugin.EarthNetworksStationLink"
    
    panels = NetworkConnection.panels + [
        InlinePanel("variable_mappings", label=_("Variable Mapping"), heading=_("Variable Mappings")),
    ]
    
    class Meta:
        verbose_name = "EarthNetworks Data Connection"
        verbose_name_plural = "EarthNetworks Data Connections"
    
    def get_api_client(self):
        """
        Returns the EarthNetworks API client instance.
        """
        return EarthNetworksClient()


class EarthNetworksVariableMapping(Orderable):
    connection = ParentalKey(EarthNetworksConnection, on_delete=models.CASCADE, related_name="variable_mappings")
    adl_parameter = models.ForeignKey(DataParameter, on_delete=models.CASCADE, verbose_name=_("ADL Parameter"))
    en_variable = models.CharField(max_length=255, verbose_name="EarthNetworks Variable")
    en_parameter_unit = models.ForeignKey(Unit, on_delete=models.CASCADE,
                                          verbose_name=_("EarthNetworks Parameter Unit"))
    
    panels = [
        FieldPanel("adl_parameter"),
        FieldPanel("en_variable"),
        FieldPanel("en_parameter_unit"),
    ]
    
    @property
    def source_parameter_name(self):
        """
        Returns the shortcode of the EarthNetworks variable.
        """
        return self.en_variable
    
    @property
    def source_parameter_unit(self):
        """
        Returns the unit of the EarthNetworks variable.
        """
        return self.en_parameter_unit


class EarthNetworksStationLink(StationLink):
    """
    Model representing a link to an EarthNetworks station.
    """
    en_station_id = models.CharField(max_length=255, verbose_name="EarthNetworks Station ID")
    start_date = models.DateTimeField(blank=True, null=True, validators=[validate_start_date],
                                      verbose_name=_("Initial Collection start date"),
                                      help_text=_(
                                          "The date to start collection data for the first collection. "
                                          "Ignored if any data has been collected already for this station"), )
    
    panels = StationLink.panels + [
        FieldPanel("en_station_id"),
        FieldPanel("start_date"),
    ]
    
    class Meta:
        verbose_name = "EarthNetworks Station Link"
        verbose_name_plural = "EarthNetworks Stations Link"
    
    def __str__(self):
        return f"{self.en_station_id} - {self.station} - {self.station.wigos_id}"
    
    def get_variable_mappings(self):
        """
        Returns the variable mappings for this station link.
        """
        return self.network_connection.variable_mappings.all()
    
    def get_first_collection_date(self):
        """
        Returns the first collection date for this station link.
        Returns None if no start date is set.
        """
        return self.start_date
