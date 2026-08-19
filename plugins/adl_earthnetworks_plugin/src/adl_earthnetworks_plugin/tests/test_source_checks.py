"""
Tests for the ingestion-diagnostic contracts this plugin asserts:
``get_source_endpoint()``, ``check_station_source()``, the
``adl_sources_count`` duck-typed handover, and the exception stamping in
``client.py``. See the "Ingestion Diagnostic Contracts" page in the ADL
developer guide.

``check_source()`` is deliberately not overridden here and has no tests:
asserting that core's default still reports UNSUPPORTED would pin *core's*
behaviour from a plugin's suite. The reason it declines is stated on
``EarthNetworksConnection`` itself.

All tests run without touching the database: model instances are built unsaved
and the HTTP layer is stubbed, so the seam under test is exactly the contract
core consumes. That is what ``SimpleTestCase`` buys here — Django still calls
``setup_databases()`` whatever the class, so the suite is run on this plugin's
own compose stack with ``make test`` from the repo root.
"""

import ast
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests
from adl.core.source_checks import SourceCheckResult, SourceCheckStatus
from django.test import SimpleTestCase

from adl_earthnetworks_plugin.client import (
    EarthNetworksClient,
    EarthNetworksConfig,
    category_for_status,
)
from adl_earthnetworks_plugin.models import EarthNetworksConnection, EarthNetworksStationLink
from adl_earthnetworks_plugin.plugins import EarthNetworksPlugin

API_HOST = "owc.enterprise.earthnetworks.com"

NOT_JSON = object()


class FakeResponse:
    """A stubbed ``requests`` response: status code, and a body that either
    parses or does not."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload

    def json(self):
        if self.payload is NOT_JSON:
            # What an error page reached through a redirect looks like from
            # here. requests' own JSONDecodeError is a ValueError too.
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeAPIClient:
    """A stubbed EarthNetworks client that answers the one call a check makes."""

    def __init__(self, data=None, error=None, normalized=None):
        self.data = data if data is not None else {}
        self.error = error
        self.normalized = normalized
        self.windows = []

    def fetch_raw(self, station_id, start_utc, end_utc):
        self.windows.append((start_utc, end_utc))
        if self.error is not None:
            raise self.error
        return self.data

    def get_data(self, station_id, start_utc=None, end_utc=None):
        if self.error is not None:
            raise self.error
        return self.normalized


def payload(station=None, observations=None, code=None):
    body = {"Result": {}}
    if station is not None:
        body["Result"]["Station"] = station
    if observations is not None:
        body["Result"]["HistoricalObservations"] = observations
    if code is not None:
        body["Code"] = code
    return body


def station_record(name="Wad Medani", inactive=None):
    record = {"StationId": "1234", "StationName": name}
    if inactive is not None:
        record["Inactive"] = inactive
    return record


def observation(minute=0):
    return {"Observation": {"ObservationTimeUtc": f"2026-08-01T00:{minute:02d}:00Z",
                            "TemperatureC": 21.5}}


def make_connection(**kwargs):
    return EarthNetworksConnection(**kwargs)


def make_station_link(connection=None, **kwargs):
    kwargs.setdefault("en_station_id", "1234")
    link = EarthNetworksStationLink(**kwargs)
    link.network_connection = connection or make_connection()
    return link


def make_client(**kwargs):
    return EarthNetworksClient(EarthNetworksConfig(**kwargs))


def stub_api_client(client):
    """Patch the client factory, capturing the arguments the check passed."""
    calls = []

    def factory(self, **kwargs):
        calls.append(kwargs)
        return client

    patcher = mock.patch.object(EarthNetworksConnection, "get_api_client", autospec=True,
                                side_effect=factory)
    return patcher, calls


class GetApiClientTests(SimpleTestCase):
    """The factory's defaults are the ingestion path's behaviour, unchanged;
    only the on-demand check asks for anything else."""

    def test_defaults_are_todays_ingestion_behaviour(self):
        client = make_connection().get_api_client()
        self.assertEqual(client.cfg.timeout, 15.0)
        self.assertEqual(client.cfg.max_retries, 3)

    def test_the_check_can_bound_the_call(self):
        client = make_connection().get_api_client(timeout=5, retries=0)
        self.assertEqual(client.cfg.timeout, 5)
        self.assertEqual(client.cfg.max_retries, 0)


class GetSourceEndpointTests(SimpleTestCase):

    def test_names_the_host_the_client_dials(self):
        # No model field configures it, but the constant is the literal string
        # requests dials — and with check_source() declining, this is what keeps
        # the connection's layer 4 answerable at all.
        self.assertEqual(make_connection().get_source_endpoint(), (API_HOST, 443))

    def test_the_host_comes_from_the_clients_own_base_url(self):
        endpoint_host, _port = make_connection().get_source_endpoint()
        self.assertIn(endpoint_host, EarthNetworksConfig.base_url)


class CheckStationSourceTests(SimpleTestCase):

    def run_check(self, client, link=None):
        link = link or make_station_link()
        patcher, calls = stub_api_client(client)
        with patcher:
            result = link.check_station_source()
        self.assertIsInstance(result, SourceCheckResult)
        self.assertIn(result.status, SourceCheckStatus.ALL)
        return result, calls

    def test_a_present_station_is_ok_with_the_upstream_label(self):
        client = FakeAPIClient(data=payload(station=station_record(),
                                            observations=[observation()]))
        result, _calls = self.run_check(client)
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("1234", result.message)
        self.assertIn("Wad Medani", result.message)

    def test_a_present_station_without_a_label_still_reads_cleanly(self):
        client = FakeAPIClient(data=payload(station={"StationId": "1234"}))
        result, _calls = self.run_check(client)
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("1234", result.message)

    def test_no_observations_in_the_window_is_still_ok(self):
        # The window is a minute wide, so an empty observation list is
        # legitimately empty and proves nothing either way.
        client = FakeAPIClient(data=payload(station=station_record(), observations=[]))
        result, _calls = self.run_check(client)
        self.assertEqual(result.status, SourceCheckStatus.OK)

    def test_an_inactive_station_is_ok_with_the_flag_stated(self):
        # Existing but disabled upstream is the operator's call, not the
        # check's.
        client = FakeAPIClient(data=payload(station=station_record(inactive=True)))
        result, _calls = self.run_check(client)
        self.assertEqual(result.status, SourceCheckStatus.OK)
        self.assertIn("inactive", result.message)

    def test_an_absent_station_block_is_proven_not_found(self):
        for body in (payload(), payload(station={}), payload(observations=[])):
            with self.subTest(body=body):
                result, _calls = self.run_check(FakeAPIClient(data=body))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertEqual(result.category, "PATH_NOT_FOUND")
                self.assertIn("1234", result.message)

    def test_asks_for_the_narrowest_window_and_bounds_the_call(self):
        client = FakeAPIClient(data=payload(station=station_record()))
        _result, calls = self.run_check(client)
        self.assertEqual(calls, [{"timeout": 5, "retries": 0}])
        start, end = client.windows[0]
        self.assertEqual(end - start, timedelta(minutes=1))

    def test_classifies_from_the_status_the_server_sent(self):
        for status, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                                 (404, "PATH_NOT_FOUND"), (503, "PROTOCOL_ERROR")):
            with self.subTest(status=status):
                error = requests.HTTPError(response=FakeResponse(status))
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertEqual(result.category, category)

    def test_an_in_body_error_code_keeps_its_category(self):
        error = RuntimeError("EarthNetworks returned non-200 Code: 401")
        error.adl_category = "AUTH_FAILED"
        error.adl_layer = 5
        result, _calls = self.run_check(FakeAPIClient(error=error))
        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertEqual(result.category, "AUTH_FAILED")

    def test_an_unclassified_in_body_code_still_fails(self):
        result, _calls = self.run_check(
            FakeAPIClient(error=RuntimeError("EarthNetworks returned non-200 Code: 999")))
        self.assertEqual(result.status, SourceCheckStatus.FAILED)
        self.assertIsNone(result.category)

    def test_a_failed_read_is_never_converted_into_ok(self):
        for error in (requests.ConnectionError("connection refused"),
                      requests.exceptions.ReadTimeout("timed out"),
                      ValueError("The response carried no result.")):
            with self.subTest(error=type(error).__name__):
                result, _calls = self.run_check(FakeAPIClient(error=error))
                self.assertEqual(result.status, SourceCheckStatus.FAILED)
                self.assertNotEqual(result.category, "PATH_NOT_FOUND")

    def test_core_detects_the_override(self):
        from adl.core.source_checks import station_link_implements_check_station_source
        self.assertTrue(station_link_implements_check_station_source(make_station_link()))


class SourcesCountTests(SimpleTestCase):
    """The count is committed only from something the source told us, and only
    once it has told us."""

    START = datetime(2026, 8, 1, tzinfo=timezone.utc)
    END = datetime(2026, 8, 2, tzinfo=timezone.utc)

    def collect(self, link, client):
        patcher, _calls = stub_api_client(client)
        with patcher:
            return EarthNetworksPlugin().get_station_data(link, self.START, self.END)

    def test_counts_the_bundles_the_response_carried(self):
        link = make_station_link()
        records = self.collect(link, FakeAPIClient(normalized=({}, [{"a": 1}, {"a": 2}], 2)))
        self.assertEqual(link.adl_sources_count, 2)
        self.assertEqual(len(records), 2)

    def test_an_empty_answer_is_zero_not_silence(self):
        link = make_station_link()
        self.collect(link, FakeAPIClient(normalized=({}, [], 0)))
        self.assertEqual(link.adl_sources_count, 0)

    def test_a_failed_call_makes_no_claim_at_all(self):
        # None, never 0: a run that never got an answer must not accuse the
        # source of having offered nothing.
        link = make_station_link()
        link.adl_sources_count = None
        with self.assertRaises(requests.ConnectionError):
            self.collect(link, FakeAPIClient(error=requests.ConnectionError("refused")))
        self.assertIsNone(link.adl_sources_count)

    def test_the_count_is_taken_before_the_conversion_loop(self):
        # Two bundles offered, one of which our own conversion drops for a
        # missing timestamp. A count of one would read as a half-empty source.
        body = payload(station=station_record(),
                       observations=[observation(), {"Observation": {"TemperatureC": 9.9}}])
        _meta, records, count = make_client().normalize(body)
        self.assertEqual(count, 2)
        self.assertEqual(len(records), 1)


class ExceptionStampingTests(SimpleTestCase):
    """A failed ingestion run carries the source's own verdict into the
    activity log, stamped in place so core's type table still applies."""

    def fetch(self, response):
        client = make_client()
        with mock.patch.object(client.session, "get", return_value=response):
            return client.fetch_raw("1234", datetime(2026, 8, 1, tzinfo=timezone.utc),
                                    datetime(2026, 8, 2, tzinfo=timezone.utc))

    def test_stamps_a_classified_status_at_layer_5(self):
        for status, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                                 (404, "PATH_NOT_FOUND"), (502, "PROTOCOL_ERROR")):
            with self.subTest(status=status):
                with self.assertRaises(requests.HTTPError) as caught:
                    self.fetch(FakeResponse(status))
                self.assertEqual(caught.exception.adl_category, category)
                self.assertEqual(caught.exception.adl_layer, 5)

    def test_leaves_a_declined_status_unstamped(self):
        # Declining keeps core's read-time tier free to classify the row later;
        # a stamp — UNKNOWN above all — would block it permanently.
        for status in (400, 422, 429):
            with self.subTest(status=status):
                with self.assertRaises(requests.HTTPError) as caught:
                    self.fetch(FakeResponse(status))
                self.assertFalse(hasattr(caught.exception, "adl_category"))

    def test_captures_the_in_body_code_while_it_is_still_an_integer(self):
        # A 200 carrying the source's own error code. Core matches on type and
        # never on text, so the code has to leave the raise site as an
        # attribute, not inside the message.
        for code, category in ((401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
                               (404, "PATH_NOT_FOUND"), (503, "PROTOCOL_ERROR")):
            with self.subTest(code=code):
                with self.assertRaises(RuntimeError) as caught:
                    self.fetch(FakeResponse(200, payload(code=code)))
                self.assertEqual(caught.exception.adl_category, category)
                self.assertEqual(caught.exception.adl_layer, 5)

    def test_an_unrecognised_body_code_fails_without_a_stamp(self):
        for code in (999, "not-a-number"):
            with self.subTest(code=code):
                with self.assertRaises(RuntimeError) as caught:
                    self.fetch(FakeResponse(200, payload(code=code)))
                self.assertFalse(hasattr(caught.exception, "adl_category"))

    def test_a_body_code_of_200_is_not_an_error(self):
        body = payload(station=station_record(), code=200)
        self.assertEqual(self.fetch(FakeResponse(200, body)), body)

    def test_core_reads_the_stamp(self):
        from adl.core.classification import classify_failure
        with self.assertRaises(requests.HTTPError) as caught:
            self.fetch(FakeResponse(401))
        self.assertEqual(classify_failure(caught.exception), ("AUTH_FAILED", 5))

        with self.assertRaises(RuntimeError) as caught:
            self.fetch(FakeResponse(200, payload(code=401)))
        self.assertEqual(classify_failure(caught.exception), ("AUTH_FAILED", 5))

    def test_a_body_that_is_not_a_result_raises(self):
        for body in (NOT_JSON, [], "ok"):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    self.fetch(FakeResponse(200, body))

    def test_the_status_table_declines_what_is_not_the_sources_fault(self):
        self.assertIsNone(category_for_status(302))
        self.assertIsNone(category_for_status(429))
        self.assertEqual(category_for_status(404), "PATH_NOT_FOUND")


class OlderCoreImportSafetyTests(SimpleTestCase):
    """The plugin must import cleanly on a core release that predates the
    source-check contracts, so nothing may import ``adl.core.source_checks``
    at module level.

    The contracts import it lazily instead, inside the method that needs it.
    Never wrap that import in ``try/except ImportError``: on an older core the
    method is never called, so the handler is unreachable, and it would turn a
    genuine import failure into a silent "this plugin does not support the
    check".
    """

    # Every module this plugin ships. Extend it as the plugin grows more.
    MODULES = ["models.py", "plugins.py", "client.py", "apps.py", "views.py",
               "validators.py", "wagtail_hooks.py"]

    DENIED = "adl.core.source_checks"

    def test_no_module_level_import_of_source_checks(self):
        package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in self.MODULES:
            path = os.path.join(package_dir, name)
            if not os.path.exists(path):
                continue  # a module this plugin does not (yet) ship
            with open(path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if node.col_offset != 0:
                    continue  # indented imports are lazy, inside a function
                names = [a.name for a in node.names]
                module = getattr(node, "module", "") or ""
                self.assertNotIn(
                    self.DENIED, [module] + names,
                    f"{name} imports {self.DENIED} at module level")
