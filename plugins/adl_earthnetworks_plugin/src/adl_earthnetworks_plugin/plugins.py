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
        
        station_meta, records = en_http_client.get_data(
            station_link.en_station_id,
            start_utc=start_date,
            end_utc=end_date
        )
        
        return records
