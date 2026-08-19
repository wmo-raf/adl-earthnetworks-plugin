from datetime import timedelta

from adl.core.registries import Plugin


class EarthNetworksPlugin(Plugin):
    type = "adl_earthnetworks_plugin"
    label = "ADL EarthNetworks Plugin"

    def get_default_start_date(self, station_link):
        end_date = self.get_default_end_date(station_link)
        # set to end_date of the previous hour
        start_date = end_date - timedelta(days=1)
        return start_date

    def get_start_date_from_db(self, station_link):
        start_date = super().get_start_date_from_db(station_link)

        if start_date:
            # add 1 minute to ensure we don't fetch already existing data
            start_date += timedelta(minutes=1)

        return start_date

    def get_station_data(self, station_link, start_date=None, end_date=None):
        en_http_client = station_link.network_connection.get_api_client()

        station_meta, records, sources_count = en_http_client.get_data(
            station_link.en_station_id,
            start_utc=start_date,
            end_utc=end_date
        )

        # Duck-typed sources-count handover: core stores this on the run's
        # activity log so "looked, found nothing" (0) stays distinguishable from
        # "never looked" (None). Committed only here, once the response is
        # parsed — a call that raised leaves the attribute None, and core's
        # evidence rule abstains on NULL rather than blaming the source for a
        # run that never got an answer. The accumulate idiom is used even though
        # this plugin makes one call: there is no straight-assignment variant.
        if getattr(station_link, "adl_sources_count", None) is None:
            station_link.adl_sources_count = 0
        station_link.adl_sources_count += sources_count

        return records
