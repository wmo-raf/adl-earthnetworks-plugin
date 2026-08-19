from datetime import timedelta
from urllib.parse import urlparse

import requests
from adl.core.models import NetworkConnection, StationLink, DataParameter, Unit
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy as _
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, InlinePanel
from wagtail.models import Orderable

from .client import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    EarthNetworksClient,
    EarthNetworksConfig,
    category_for_status,
)
from .validators import validate_start_date

# What the diagnostic's on-demand check passes instead of the ingestion
# defaults. Core bounds its whole probe — DNS, TCP and the check together — by a
# 15-second wall clock and abandons rather than kills a worker that overruns it.
# This client's own 15-second timeout with three backed-off retries can consume
# that budget several times over. Deliberately not a model field: an operator
# who raised it to 300 for a slow partner would silently re-break the probe.
SOURCE_CHECK_TIMEOUT_SECONDS = 5
SOURCE_CHECK_RETRIES = 0

# The narrowest window the station check can ask for. The point of the call is
# to prove the station resolves, not to fetch data: station identity comes back
# under Result.Station whatever the window returns, so a minute costs nothing
# and an empty observation list under it is legitimately empty.
SOURCE_CHECK_WINDOW = timedelta(minutes=1)


class EarthNetworksConnection(NetworkConnection):
    """
    Model representing a connection to EarthNetworks Data.

    `check_source()` is deliberately **not** overridden here, so core reports
    layer 5 as UNSUPPORTED at connection scope. That is a decision, not an
    omission: this connection holds no credential of any kind, and the API
    offers no station-independent call — every request needs a station id and a
    time window. Both ways of faking one were rejected. Borrowing the first
    enabled station link would report a station-specific fault as a
    whole-connection failure, which is the misattribution the layer model exists
    to prevent; an unauthenticated liveness GET would prove only what core's TCP
    step already proved, while looking like a real source check.

    The connection is still covered where it can be: it names its endpoint for
    core's layer-4 probe below, and each station link answers the real
    station-scoped check.
    """
    station_link_model_string_label = "adl_earthnetworks_plugin.EarthNetworksStationLink"

    panels = NetworkConnection.panels + [
        InlinePanel("variable_mappings", label=_("Variable Mapping"), heading=_("Variable Mappings")),
    ]

    class Meta:
        verbose_name = "EarthNetworks Data Connection"
        verbose_name_plural = "EarthNetworks Data Connections"

    @property
    def source_host(self):
        """The data host this connection dials, for operator-facing messages."""
        return urlparse(EarthNetworksConfig.base_url).hostname

    def get_api_client(self, use_cache=True, timeout=DEFAULT_TIMEOUT,
                       retries=DEFAULT_MAX_RETRIES):
        """
        Returns the EarthNetworks API client instance.

        The defaults are the ingestion path's behaviour, unchanged. The
        diagnostic's on-demand station check passes a bounded client instead.
        Nothing in this client caches, so `use_cache` is accepted for a uniform
        factory signature and has nothing to bypass — verified against every
        call it makes, and worth re-verifying if a cache is ever added.
        """
        return EarthNetworksClient(
            EarthNetworksConfig(timeout=timeout, max_retries=retries)
        )

    def get_source_endpoint(self):
        """
        The (host, port) core's generic DNS -> TCP probe dials (layer 4 of the
        ingestion diagnostic).

        No model field configures the host: the client's own base URL is the
        literal string requests dials, which makes naming it exactly as truthful
        as reading a field would be. Naming it matters here precisely because
        `check_source()` declines — core's support probe is an OR over the two
        surfaces, so this is what keeps the connection's layer 4 answerable.
        """
        parsed = urlparse(EarthNetworksConfig.base_url)
        return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


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

    def check_station_source(self):
        """
        Ask whether this station's EarthNetworks id resolves at the source
        (layer 5 of the ingestion diagnostic, station-scoped).

        This plugin's only station-scoped read is the ingestion call itself, so
        the check makes that call under the narrowest window the API accepts.
        The response carries station identity under Result.Station, on a
        different branch from the observations, so a one-minute window proves
        addressability and yields the upstream's own name for almost no payload.
        """
        from adl.core.source_checks import SourceCheckResult, SourceCheckStatus

        connection = self.network_connection
        host = connection.source_host

        end_utc = timezone.now()
        start_utc = end_utc - SOURCE_CHECK_WINDOW

        try:
            client = connection.get_api_client(timeout=SOURCE_CHECK_TIMEOUT_SECONDS,
                                               retries=SOURCE_CHECK_RETRIES)
            data = client.fetch_raw(self.en_station_id, start_utc, end_utc)
        except requests.HTTPError as e:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                category=category_for_status(e.response.status_code),
                message=gettext("%(host)s returned HTTP %(code)s for station "
                                "%(station)s.") % {
                    "host": host,
                    "code": e.response.status_code,
                    "station": self.en_station_id,
                },
            )
        except RuntimeError as e:
            # The source answered 200 with its own error code in the body. The
            # category was captured at the raise site while the code was still
            # an integer; there is nothing else in this client that raises this
            # type.
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                category=getattr(e, "adl_category", None),
                message=gettext("%(host)s reported an error for station "
                                "%(station)s: %(error)s") % {
                    "host": host,
                    "station": self.en_station_id,
                    "error": e,
                },
            )
        except ValueError:
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("%(host)s answered, but the response was not an "
                                "observation payload.") % {
                    "host": host,
                },
            )
        except requests.RequestException as e:
            # Never convert a failed read into OK — and never into a claim of
            # absence either, which we have no proof of here.
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                message=gettext("Could not read station %(station)s from %(host)s: "
                                "%(error)s") % {
                    "station": self.en_station_id,
                    "host": host,
                    "error": e,
                },
            )

        station = (data.get("Result") or {}).get("Station") or {}

        if not station:
            # An empty station block against a 200 the source really sent is
            # proof, not suspicion: this station link can never ingest anything.
            # An empty observation list would prove nothing of the sort, which
            # is why identity is read and the observations are not.
            return SourceCheckResult(
                status=SourceCheckStatus.FAILED,
                category="PATH_NOT_FOUND",
                message=gettext("Station %(station)s was not found at the source.") % {
                    "station": self.en_station_id,
                },
            )

        # The upstream's own label is what catches a valid-but-wrong id — a real
        # station belonging to a different site — which is the failure that
        # yields plausible wrong data rather than an outage.
        label = station.get("StationName") or ""

        if label:
            message = gettext('Station %(station)s found upstream as "%(label)s".') % {
                "station": self.en_station_id,
                "label": label,
            }
        else:
            message = gettext("Station %(station)s was found at the source.") % {
                "station": self.en_station_id,
            }

        if station.get("Inactive"):
            # Existing but disabled upstream is the operator's call, not this
            # check's, so it stays OK with the flag stated.
            message = gettext("%(message)s The source reports it as inactive.") % {
                "message": message,
            }

        return SourceCheckResult(status=SourceCheckStatus.OK, message=message)
